"""
ContigSequence.py contains the class declaration for ContigSequence
and required biological constants such as STOP_CODONS and START_CODONS.

# Some notes about BLASTX output.

Alignments with a negative frame (that is the reverse complement of
the contig maps to the subject protein), have a `query_start` that
corresponds to the *end* of the subject protein, and the `query_end`
is the 5'-most to the subject protein. So for frame < 0, `query_end`
is the 5'-most.

"""

import sys
import pdb
from collections import Counter, namedtuple, defaultdict
from string import Template
from operator import itemgetter, attrgetter

try:
    from Bio.Blast import NCBIXML
    from Bio import SeqIO
    from Bio.Seq import Seq
except ImportError, e:
    sys.exit("Cannot import BioPython modules; please install it.")

from rules import HSP, ORF, AnchorHSPs
from rules import get_anchor_HSPs
import templates

GTF_FIELDS = ("seqname", "source", "feature", "start",
              "end", "score", "strand", "frame", "group")



class ContigSequence():
    """
    ContigSequence represents an assembled contig, that may be coding
    or non-coding. It contains all information about the its sequence
    and the blastx results to its relatives.
    """

    def __init__(self, query_id, sequence):
        """
        Initialize a ContigSequence with a contig ID and sequence. The
        contig ID must correspond to the same one used in the blastx
        results.
        """
        # core data attributes
        self.query_id = query_id
        self.seq = sequence
        self.len = len(sequence)

        # information added by blastx results
        self.all_relatives = defaultdict(list)

        # ORF and annotation attributes
        self.orf = None
        self.all_orfs = None
        self.annotation = dict()

    def __repr__(self):

        info = dict(id=self.query_id,
                    length=self.len,
                    num_relatives=self.num_relatives,
                    majority_frame=self.majority_frame,
                    majority_frameshift=self.majority_frameshift,
                    missing_start=self.get_annotation('missing_start'),
                    missing_stop=self.get_annotation('missing_stop'),
                    missing_5prime=self.missing_5prime(self.get_anchor_HSPs()),
                    full_length_orf=self.get_annotation('full_length'),
                    orf_start=self.orf.query_start if self.orf is not None else None,
                    orf_end=self.orf.query_end if self.orf is not None else None,
                    seq=None)
        
        out = Template(templates.contig_sequence_repr).substitute(info)
        return out
    

    def get_annotation(self, key):
        """
        Get annotation from key, or return None if it doesn't exist.
        """
        return self.annotation.get(key, None)

    def add_orf_prediction(self, orf):
        """
        Add a ORF, which is a ORF named tuple.
        """
        self.orf = orf

    def add_annotation(self, annotation):
        """
        Annotation is just a dictinary of key-value pairs; this append
        those (or updates them if they exist)
        """
        self.annotation = dict(self.annotation.items() + annotation.items())
        

    def get_relatives(self, e_value=None, pi_range=None):
        """
        Return relatives that pass thresholding filters.
        
        The `add_relative` method adds relatives' HSPs to a dictionary
        attribute, `all_relatives`. However, in most cases, we want to
        use a subset of these relatives that satisfy requirements
        based on phylogenetic requirements, i.e. requiring a relative HSP
        have a percent identity consistent with evolutionary distance.

        If `e_value` or `pi_range` are None, they are not used for
        filtering `all_relatives`.
        """
        if e_value is None and pi_range is None:
            return self.all_relatives

        # little funcs for e-value filtering
        e_thresh = lambda x: x.e <= e_value

        filtered_relatives = defaultdict(list)
        for relative, hsps in self.all_relatives.items():

            filters = [(e_value, e_thresh)]
            # make a custom filter closure for this relative's range;
            # if a relative's range is None, we don't filter on it.
            
            if pi_range is not None:
                rng = pi_range[relative]
                in_range = (lambda x:
                            rng is None or rng[0] <= x.percent_identity <= rng[1])
                filters.append((pi_range, in_range))

            for h in hsps:
                if all([fun(h) for arg, fun in filters if arg is not None]):
                    filtered_relatives[relative].append(h)

        return filtered_relatives

    @property
    def num_relatives(self):
        """
        Return the number of relatives.
        """
        num_relatives = 0
        for relative, hsps in self.all_relatives.items():
            if len(hsps) > 0:
                num_relatives += 1
        return num_relatives

    @property
    def has_relatives(self):
        """
        Return True or False depending on whether the number of
        relatives is greater than 0.

        """
        return self.num_relatives > 0
        
        
    def gff_dict(self):
        """
        Return a dictionary of some key attribute's values,
        corresponding to a GFF file's columns.

        Note that GFFs are 1-indexed, so we add one to positions.
        """
        out = dict()
        out["seqname"] = self.query_id
        out["source"] = "findorf"
        out["feature"] = "predicted_orf"
        out["start"] = self.orf_start + 1 if self.orf_start is not None else "."
        out["end"] = self.orf_end + 1 if self.orf_end is not None else "."
        out["score"] = "."

        if self.majority_frameshift is not None:
            out["strand"] = self.majority_frame/abs(self.majority_frame)
        else:
            out["strand"] = "."

        if self.majority_frame is not None:
            # GFF uses frames in [0, 2]
            out["frame"] = abs(self.majority_frame) - 1
        else:
             out["frame"] = "."
        out["group"] = "."
        return out

    def gtf_dict(self):
        """
        Return a dictionary corresponding to the columns of a GTF
        file.
        """

        # a GTF's file's "group" column contains a merged set of
        # attributes, which in ContigSequence's case are those below
        attributes = dict(full_length_orf=self.full_length_orf,
                          majority_frameshift=self.majority_frameshift,
                          any_frameshift=self.any_frameshift,
                          missing_5prime=self.missing_5prime,
                          number_relatives=len(self.relatives))

        group = "; ".join(["%s %s" % (k, v) for k, v in attributes.iteritems()])
        out = self.gff_dict()
        out["group"] = group
        return out

    def add_relative_alignment(self, relative, blast_record):
        """
        Given a relative and a BioPython BLAST alignment objects,
        extract and store the relevant parts of the _best_ alignment
        only.
        """
        if len(blast_record.alignments) == 0:
            # no alignments, so we dont have any info to add for this
            # relative.
            return 

        # TODO check: are these guaranteed in best first order?
        best_alignment = blast_record.alignments[0]
        for hsp in best_alignment.hsps:
            percent_identity = hsp.identities/float(hsp.align_length)

            # the BioPython parser doesn't give us a non-zero second
            # frame (which is for use with non-blastx parsers).
            assert(hsp.frame[1] is 0)

            # blastx has protein subjects, so this should always be the case
            assert(hsp.sbjct_start < hsp.sbjct_end)
            
            hsp = HSP(e=hsp.expect,
                      identities=hsp.identities,
                      length=hsp.align_length,
                      percent_identity=percent_identity,
                      title=best_alignment.title,
                      query_start=hsp.query_start,
                      query_end=hsp.query_end,
                      sbjct_start=hsp.sbjct_start,
                      sbjct_end=hsp.sbjct_end,
                      frame=hsp.frame[0])

            self.all_relatives[relative].append(hsp)

    @property
    def frames(self):
        """
        Calculate and return the identity counts by relative and
        frame. This is used for both frameshift and any_frameshift.

        """

        frame_counts = defaultdict(Counter)
        for relative, hsps in self.all_relatives.iteritems():
            # count the number of identities per each relative's HSP
            for h in hsps:
                f = h.frame
                frame_counts[relative][f] += h.identities

        return frame_counts

    @property
    def majority_frame(self):
        """
        The `majority_frame` attribute indicates the majority, based
        on the number of *identities* that agree on a frame.
        
        This has the advantage that longer HSPs are weighted more
        heavily in the calculations. Furthermore, more distant
        relatives will likely be more divergent in terms of protein
        identity, so this provides a natural way of weighting by
        evolutionary distance.
        """
        if not self.has_relatives:
            return None

        frame = Counter()
        for relative, counts in self.frames.items():
            if len(counts) == 1:
                # no frameshifts in this relative
                frame[counts.keys()[0]] += sum(counts.values())

        if len(frame):
            majority_frame, count = frame.most_common(1)[0]
            return majority_frame

        return None
                
                
    @property
    def majority_frameshift(self):
        """
        Returns True of False if there's a frameshift in the majority
        of relatives, weighted by their identities.
        
        """

        if not self.has_relatives:
            return None

        frameshifts = Counter()
        for relative, counts in self.frames.items():
            frameshifts[len(counts.keys()) > 1] += sum(counts.values())
            
        return frameshifts[True] >= frameshifts[False]

    @property
    def any_frameshift(self):
        """
        Return if there are any relatives with frameshifts.
        
        """
        if not self.has_relatives:
            return None
    
        return any([len(c.keys()) > 1 for r, c in self.frames.iteritems()])

    @property
    def is_reversed(self):
        """
        Return True of the query is reversed.
        
        """
        if not self.has_relatives:
            return None

        return self.majority_frame < 0        

    def get_anchor_HSPs(self, e_value=None, pi_range=None):
        """
        Get the 5'-most and 3'-most HSPs for each relative and put
        them in a tuple.

        Note that we have to take into account that a query that
        mapped in the reverse complemented configuration must be
        flipped for calculating this. In this case, the `query_end`,
        must be minimized, not the `query_subject`.

        We a generic `get_anchor_HSPs` method here, both because this
        method is useful outside of the `ContigSequence` class and
        because I wanted to unit test it outside of the
        `ContigSequence` class.
        """
        # TODO add unit tests for this.

        return get_anchor_HSPs(self.get_relatives(e_value, pi_range), self.is_reversed)

    def missing_5prime(self, anchor_hsps, qs_thresh=16, ss_thresh=40):
        """
        Return True if the anchor_hsps indicate a missing 5'-end of
        this contig.

        `qs_start` and `ss_thresh` are in amino acids.
        Each HSP has a query start and a subject start. A missing
        5'-end would look like this (in the case that the HSP spans
        the missing part):
        
                       query start
                      |   HSP
                 |------------------------------------------| contig
                      |||||||||||
              |.......|---------| subject
           subject
            start
        
        We infer missing 5'-end based on the query start position
        (compared to a threshold, `qs_thresh`) and the subject start
        position (`ss_thresh`). Starting late in the subject and early
        in the query probably means we're missing part of a protein.
        
        """
        
        missing_5prime = Counter()

        for relative, hsps in anchor_hsps.iteritems():
            most_5prime, most_3prime, strand = hsps
            # note that for reverse strand: query_start is really
            # query_end, but we compare it to the difference between
            # query_end and query_length.
            query_start = most_5prime.query_start
            sbjct_start = most_5prime.sbjct_start

            if strand > 0:
                m = query_start <= qs_thresh and sbjct_start >= ss_thresh
                missing_5prime[m] += 1
            else:
                # takte the query start and subtract it from length to
                # put everything on forward strand.
                qs = abs(query_start - self.len) + 1 # blast results are 1-indexed
                m = qs <= qs_thresh and sbjct_start >= ss_thresh
                missing_5prime[m] += 1
                  
        return missing_5prime[True] >= missing_5prime[False]
