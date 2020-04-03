import parasail
from .._util._multiprocessing import EnhancedPool as Pool
import itertools
from anndata import AnnData
from typing import Union, Collection, List, Tuple, Dict, Callable
from .._compat import Literal
import numpy as np
from scanpy import logging
import numpy.testing as npt
from .._util import _is_na, _is_symmetric, _reduce_nonzero
import abc
from Levenshtein import distance as levenshtein_dist
import scipy.spatial
import scipy.sparse
from scipy.sparse import coo_matrix, csr_matrix, lil_matrix
from functools import reduce
from collections import Counter


class _DistanceCalculator(abc.ABC):
    DTYPE = "uint8"

    def __init__(self, cutoff: float, n_jobs: Union[int, None] = None):
        """
        Parameters
        ----------
        cutoff:
            Will eleminate distances > cutoff to make efficient 
            use of sparse matrices. 
        n_jobs
            Number of jobs to use for the pairwise distance calculation. 
            If None, use all jobs. 
        """
        if cutoff > 255:
            raise ValueError(
                "Using a cutoff > 255 is not possible due to the `uint8` dtype used"
            )
        self.cutoff = cutoff
        self.n_jobs = n_jobs

    @abc.abstractmethod
    def calc_dist_mat(self, seqs: np.ndarray) -> coo_matrix:
        """Calculate the upper diagnoal, pairwise distance matrix of all 
        sequences in `seq`.

         * Only returns distances <= cutoff
         * Distances are non-negative values.
         * The resulting matrix is offsetted by 1 to allow efficient use
           of sparse matrices ($d' = d+1$).
           I.e. 0 -> d > cutoff; 1 -> d == 0; 2 -> d == 1; ...
        """
        pass


class _IdentityDistanceCalculator(_DistanceCalculator):
    """Calculate the distance between TCR based on the identity 
    of sequences. I.e. 0 = sequence identical, 1 = sequences not identical
    """

    def __init__(self, cutoff: float = 0, n_jobs: Union[int, None] = None):
        """For this DistanceCalculator, per definition, the cutoff = 0. 
        The `cutoff` argument is ignored. """
        super().__init__(cutoff, n_jobs)

    def calc_dist_mat(self, seqs: np.ndarray) -> coo_matrix:
        """The offsetted matrix is the identity matrix."""
        return scipy.sparse.identity(len(seqs), dtype=self.DTYPE, format="coo")


class _LevenshteinDistanceCalculator(_DistanceCalculator):
    """Calculates the Levenshtein (i.e. edit-distance) between sequences. """

    def _compute_row(self, seqs: np.ndarray, i_row: int) -> coo_matrix:
        """Compute a row of the upper diagnomal distance matrix"""
        target = seqs[i_row]

        def coord_generator():
            for j, s2 in enumerate(seqs[i_row:], start=i_row):
                d = levenshtein_dist(target, s2)
                if d <= self.cutoff:
                    yield d + 1, j

        d, col = zip(*coord_generator())
        row = np.zeros(len(col), dtype="int")
        return coo_matrix((d, (row, col)), dtype=self.DTYPE, shape=(1, seqs.size))

    def calc_dist_mat(self, seqs: np.ndarray) -> csr_matrix:
        p = Pool(self.n_jobs)
        rows = p.starmap_progress(
            self._compute_row,
            zip(itertools.repeat(seqs), range(len(seqs))),
            chunksize=200,
            total=len(seqs),
        )
        p.close()

        score_mat = scipy.sparse.vstack(rows)
        score_mat.eliminate_zeros()
        assert score_mat.shape[0] == score_mat.shape[1]

        return score_mat


class _AlignmentDistanceCalculator(_DistanceCalculator):
    """Calculates distance between sequences based on pairwise sequence alignment. 

    The distance between two sequences is defined as $S_{1,2}^{max} - S_{1,2}$ 
    where $S_{1,2} $ is the alignment score of sequences 1 and 2 and $S_{1,2}^{max}$ 
    is the max. achievable alignment score of sequences 1 and 2 defined as 
    $\\min(S_{1,1}, S_{2,2})$. 
    """

    def __init__(
        self,
        cutoff: float,
        n_jobs: Union[int, None] = None,
        *,
        subst_mat: str = "blosum62",
        gap_open: int = 11,
        gap_extend: int = 1,
    ):
        """Class to generate pairwise alignment distances
        
        High-performance sequence alignment through parasail library [Daily2016]_

        Parameters
        ----------
        cutoff
            see `_DistanceCalculator`
        n_jobs
            see `_DistanceCalculator`
        subst_mat
            Name of parasail substitution matrix
        gap_open
            Gap open penalty
        gap_extend
            Gap extend penatly
        """
        super().__init__(cutoff, n_jobs)
        self.subst_mat = subst_mat
        self.gap_open = gap_open
        self.gap_extend = gap_extend

    def _align_row(
        self, seqs: np.ndarray, self_alignment_scores: np.array, i_row: int
    ) -> np.ndarray:
        """Generates a row of the triangular distance matrix. 
        
        Aligns `seqs[i_row]` with all other sequences in `seqs[i_row:]`. 

        Parameters
        ----------
        seqs
            Array of amino acid sequences
        self_alignment_scores
            Array containing the scores of aligning each sequence in `seqs` 
            with itself. This is used as a reference value to turn 
            alignment scores into distances. 
        i_row
            Index of the row in the final distance matrix. Determines the target sequence. 

        Returns
        -------
        The i_th row of the final score matrix. 
        """
        subst_mat = parasail.Matrix(self.subst_mat)
        target = seqs[i_row]
        profile = parasail.profile_create_16(target, subst_mat)

        def coord_generator():
            for j, s2 in enumerate(seqs[i_row:], start=i_row):
                r = parasail.nw_scan_profile_16(
                    profile, s2, self.gap_open, self.gap_extend
                )
                max_score = np.min(self_alignment_scores[[i_row, j]])
                d = max_score - r.score
                if d <= self.cutoff:
                    yield d + 1, j

        d, col = zip(*coord_generator())
        row = np.zeros(len(col), dtype="int")
        return coo_matrix((d, (row, col)), dtype=self.DTYPE, shape=(1, len(seqs)))

    def calc_dist_mat(self, seqs: Collection) -> coo_matrix:
        """Calculate the distances between amino acid sequences based on
        of all-against-all pairwise sequence alignments.

        Parameters
        ----------
        seqs
            Array of amino acid sequences

        Returns
        -------
        Upper diagonal distance matrix of normalized alignment distances. 
        """
        # first, calculate self-alignments. We need them as refererence values
        # to turn scores into dists
        self_alignment_scores = np.array(
            [
                parasail.nw_scan_16(
                    s,
                    s,
                    self.gap_open,
                    self.gap_extend,
                    parasail.Matrix(self.subst_mat),
                ).score
                for s in seqs
            ]
        )

        p = Pool(self.n_jobs)
        rows = p.starmap_progress(
            self._align_row,
            zip(
                itertools.repeat(seqs),
                itertools.repeat(self_alignment_scores),
                range(len(seqs)),
            ),
            chunksize=200,
            total=len(seqs),
        )
        p.close()

        score_mat = scipy.sparse.vstack(rows)
        score_mat.eliminate_zeros()
        assert score_mat.shape[0] == score_mat.shape[1]

        return score_mat


def tcr_dist(
    unique_seqs,
    *,
    metric: Union[
        Literal["alignment", "identity", "levenshtein"], _DistanceCalculator
    ] = "identity",
    cutoff: float = 2,
    n_jobs: Union[int, None] = None,
):
    """calculate the sequence x sequence distance matrix"""
    if isinstance(metric, _DistanceCalculator):
        dist_calc = metric
    elif metric == "alignment":
        dist_calc = _AlignmentDistanceCalculator(cutoff=cutoff, n_jobs=n_jobs)
    elif metric == "identity":
        dist_calc = _IdentityDistanceCalculator(cutoff=cutoff)
    elif metric == "levenshtein":
        dist_calc = _LevenshteinDistanceCalculator(cutoff=cutoff, n_jobs=n_jobs)
    else:
        raise ValueError("Invalid distance metric.")

    dist_mat = dist_calc.calc_dist_mat(unique_seqs)
    return dist_mat


class TcrNeighbors:
    @staticmethod
    def _seq_to_cell_idx(
        unique_seqs: np.ndarray, cdr_seqs: np.ndarray
    ) -> Dict[int, List[int]]:
        """
        Compute sequence to cell index for a single chain (e.g. `TRA_1`). 

        Maps cell_idx -> [list, of, seq_idx]. 
        Useful to build a cell x cell matrix from a seq x seq matrix. 

        Computes magic lookup indexes in linear time

        Parameters
        ----------
        unique_seqs
            Pool of all unique cdr3 sequences (length = #unique cdr3 sequences)
        cdr_seqs
            CDR3 sequences for the current chain (length = #cells)

        Returns
        -------
        Sequence2Cell mapping    
        """
        # 1) reverse mapping of amino acid sequence to index in sequence-distance matrix
        seq_to_index = {seq: i for i, seq in enumerate(unique_seqs)}

        # 2) indices of cells in adata that have a CDR3 sequence.
        cells_with_chain = np.where(~_is_na(cdr_seqs))[0]

        # 3) indices of the corresponding sequences in the distance matrix.
        seq_inds = {
            chain_id: seq_to_index[cdr_seqs[chain_id]] for chain_id in cells_with_chain
        }

        # 4) list of cell-indices in the cell distance matrix for each sequence
        seq_to_cell = {seq_id: list() for seq_id in seq_to_index.values()}
        for cell_id in cells_with_chain:
            seq_id = seq_inds[cell_id]
            seq_to_cell[seq_id].append(cell_id)

        return seq_to_cell

    def _build_index_dict(self):
        """Build nexted dictionary containing all combinations of
        receptor_arms x primary/secondary_chain"""
        receptor_arms = (
            ["TRA", "TRB"]
            if self.receptor_arms not in ["TRA", "TRB"]
            else [self.receptor_arms]
        )
        chain_inds = [1] if self.dual_tcr == "primary_only" else [1, 2]
        sequence = "" if self.sequence == "aa" else "_nt"

        arm_dict = {}
        for arm in receptor_arms:
            cdr_seqs = {
                k: self.adata.obs[f"{arm}_{k}_cdr3{sequence}"].values
                for k in chain_inds
            }
            unique_seqs = np.hstack(list(cdr_seqs.values()))
            unique_seqs = np.unique(unique_seqs[~_is_na(unique_seqs)]).astype(str)
            seq_to_cell = {
                k: self._seq_to_cell_idx(unique_seqs, cdr_seqs[k]) for k in chain_inds
            }
            # chains_per_cell = np.sum(
            #     ~_is_na(self.adata.obs.loc[:, [f"{c}_cdr3" for c in chains]]), axis=1
            # )
            arm_dict[arm] = {
                "chain_inds": chain_inds,
                "unique_seqs": unique_seqs,
                "seq_to_cell": seq_to_cell,
                # "chains_per_cell": chains_per_cell,
            }

        self.index_dict = arm_dict

    def __init__(
        self,
        adata: AnnData,
        *,
        metric: Literal["alignment", "identity", "levenshtein"] = "identity",
        cutoff: float = 0,
        receptor_arms: Literal["TRA", "TRB", "all", "any"] = "all",
        dual_tcr: Literal["primary_only", "all", "any"] = "primary_only",
        sequence: Literal["aa", "nt"] = "aa",
    ):
        if metric == "identity" and cutoff != 0:
            raise ValueError("Identity metric only works with cutoff = 0")
        if sequence == "nt" and metric == "alignment":
            raise ValueError(
                "Using nucleotide sequences with alignment metric is not supported. "
            )
        self.adata = adata
        self.metric = metric
        self.cutoff = cutoff
        self.receptor_arms = receptor_arms
        self.dual_tcr = dual_tcr
        self.sequence = sequence
        self._build_index_dict()
        self._dist_mat = None
        logging.debug("Finished initalizing TcrNeighbors object. ")

    def _build_cell_dist_mat_min(self):
        """Compute the distance matrix in-place by reducing everything
        instantly by `min`"""
        coord_dict = dict()
        for arm, arm_info in self.index_dict.items():
            dist_mat, seq_to_cell, chains = (
                arm_info["dist_mat"],
                arm_info["seq_to_cell"],
                arm_info["chains"],
            )
            for row, col, value in zip(dist_mat.row, dist_mat.col, dist_mat.data):
                for c1, c2 in itertools.product(chains, repeat=2):
                    for cell_row, cell_col in itertools.product(
                        seq_to_cell[c1][row], seq_to_cell[c2][col]
                    ):
                        try:
                            coord_dict[(cell_row, cell_col)] = min(
                                coord_dict[(cell_row, cell_col)], value
                            )
                        except KeyError:
                            coord_dict[(cell_row, cell_col)] = value
                        # build full matrix from triangular one. Only emit single
                        # value for diagonal.
                        if row != col:
                            try:
                                coord_dict[(cell_col, cell_row)] = min(
                                    coord_dict[(cell_col, cell_row)], value
                                )
                            except KeyError:
                                coord_dict[(cell_col, cell_row)] = value

        return coord_dict

    def _cell_dist_mat_reduce(
        self, reduce_arms: Callable, reduce_dual: Callable,
    ):
        """Compute the distance matrix by using custom reduction functions. 
        More flexible than `_build_cell_dist_mat_min`, but requires more memory.
        Reduce dual is called before reduce arms. 

        Parameters
        ----------
        reduce_arms:
            Function taking a list of elements and returning a single value. 
            Reduces the distances from multiple receptor arms into a single one. 
        reduce_dual:
            Function taking a list of elements and returning a single value.
            Reduces the distances from multiple chains of the same receptor arm 
            into one. 
        """
        coord_dict = dict()

        def _add_to_dict(d, c1, c2, cell_row, cell_col, value):
            try:
                tmp_dict = d[(cell_row, cell_col)]
                try:
                    tmp_dict2 = tmp_dict[arm]
                    try:
                        if (c1, c2) in tmp_dict2:
                            # can be in arbitrary order apprarently
                            assert (c2, c1) not in tmp_dict2
                            tmp_dict2[(c2, c1)] = value
                        tmp_dict2[(c1, c2)] = value
                    except KeyError:
                        tmp_dict2 = {(c1, c2): value}
                except KeyError:
                    tmp_dict[arm] = {(c1, c2): value}
            except KeyError:
                d[(cell_row, cell_col)] = {arm: {(c1, c2): value}}

        for arm, arm_info in self.index_dict.items():
            dist_mat, seq_to_cell, chain_inds = (
                arm_info["dist_mat"],
                arm_info["seq_to_cell"],
                arm_info["chain_inds"],
            )
            for row, col, value in zip(dist_mat.row, dist_mat.col, dist_mat.data):
                for c1, c2 in itertools.product(chain_inds, repeat=2):
                    for cell_row, cell_col in itertools.product(
                        seq_to_cell[c1][row], seq_to_cell[c2][col]
                    ):
                        _add_to_dict(coord_dict, c1, c2, cell_row, cell_col, value)
                        if row != col:
                            _add_to_dict(coord_dict, c1, c2, cell_col, cell_row, value)

        coord_dict = {
            coords: reduce_arms(
                reduce_dual(value_dict) for value_dict in entry.values()
            )
            for coords, entry in coord_dict.items()
        }
        return coord_dict

    def compute_distances(
        self, n_jobs: Union[int, None] = None,
    ):
        """Computes the distances between CDR3 sequences 

        Parameters
        ----------
        j_jobs
            Number of CPUs to use for alignment and levenshtein distance. 
            Default: use all CPUS. 
        """
        for arm, arm_dict in self.index_dict.items():
            arm_dict["dist_mat"] = tcr_dist(
                arm_dict["unique_seqs"],
                metric=self.metric,
                cutoff=self.cutoff,
                n_jobs=n_jobs,
            )
            logging.info("Finished computing {} pairwise distances.".format(arm))

        def _reduce_dual_all(d):
            if len(d) == 1:
                return next(iter(d.values()))
            elif len(d) == 4:
                # -1 because both distances are offseted by 1
                return min(d[(1, 2)] + d[(2, 1)], d[(1, 1)] + d[(2, 2)]) - 1
            elif len(d) == 2:
                return 0
            else:
                raise AssertionError("Can only be of length 1, 2 or 4. ")

        reduce_dual = (
            _reduce_dual_all if self.dual_tcr == "all" else lambda x: min(x.values())
        )
        reduce_arms = sum if self.receptor_arms == "all" else min
        coord_dict = self._cell_dist_mat_reduce(reduce_arms, reduce_dual)

        coords, values = zip(*coord_dict.items())
        rows, cols = zip(*coords)
        dist_mat = coo_matrix(
            (values, (rows, cols)), shape=(self.adata.n_obs, self.adata.n_obs)
        )
        dist_mat.eliminate_zeros()
        self._dist_mat = dist_mat.tocsr()

    @property
    def dist(self):
        return self._dist_mat

    @property
    def connectivities(self):
        """Get the weighted adjacecency matrix derived from the distance matrix. 

        The cutoff will be used to normalize the distances. 
        """
        if self.cutoff == 0:
            return self._dist_mat

        connectivities = self._dist_mat.copy()

        # actual distances
        d = connectivities.data - 1

        # structure of the matrix stayes the same, we can safely change the data only
        connectivities.data = (self.cutoff - d) / self.cutoff
        connectivities.eliminate_zeros()
        return connectivities


def tcr_neighbors(
    adata: AnnData,
    *,
    metric: Literal["identity", "alignment", "levenshtein"] = "alignment",
    cutoff: int = 2,
    receptor_arms: Literal["TRA", "TRB", "all", "any"] = "all",
    dual_tcr: Literal["primary_only", "any", "all"] = "primary_only",
    key_added: str = "tcr_neighbors",
    sequence: Literal["aa", "nt"] = "aa",
    inplace: bool = True,
    n_jobs: Union[int, None] = None,
) -> Union[Tuple[csr_matrix, csr_matrix], None]:
    """Construct a cell x cell neighborhood graph based on CDR3 sequence
    similarity. 

    Parameters
    ----------
    adata
        annotated data matrix
    metric
        "identity" = Calculate 0/1 distance based on sequence identity. Equals a 
            cutoff of 0. 
        "alignment" - Calculate distance using pairwise sequence alignment 
            and BLOSUM62 matrix
        "levenshtein" - Levenshtein edit distance
    cutoff
        Two cells with a distance <= the cutoff will be connected. 
        If cutoff = 0, the CDR3 sequences need to be identical. In this 
        case, no alignment is performed. 
    receptor_arms:
        "TRA" - only consider TRA sequences
        "TRB" - only consider TRB sequences
        "all" - both TRA and TRB need to match
        "any" - either TRA or TRB need to match
    dual_tcr:
        "primary_only" - only consider most abundant pair of TRA/TRB chains
        "any" - consider both pairs of TRA/TRB sequences. Distance must be below
        cutoff for any of the chains. 
        "all" - consider both pairs of TRA/TRB sequences. Distance must be below
        cutoff for all of the chains. 
    key_added:
        dict key under which the result will be stored in `adata.uns["scirpy"]`
        when `inplace` is True.
    sequence:
        Use amino acid (aa) or nulceotide (nt) sequences to define clonotype? 
    inplace:
        If True, store the results in adata.uns. If False, returns
        the results. 
    n_jobs:
        Number of cores to use for alignment and levenshtein distance. 
    
    Returns
    -------
    connectivities
        weighted adjacency matrix
    dist
        cell x cell distance matrix with the distances as computed according to `metric`
        offsetted by 1 to make use of sparse matrices. 
    """
    if cutoff == 0:
        metric = "identity"
    ad = TcrNeighbors(
        adata,
        metric=metric,
        cutoff=cutoff,
        receptor_arms=receptor_arms,
        dual_tcr=dual_tcr,
        sequence=sequence,
    )
    ad.compute_distances(n_jobs)
    logging.debug("Finished converting distances to connectivities. ")

    if not inplace:
        return ad.connectivities, ad.dist
    else:
        adata.uns[key_added] = dict()
        adata.uns[key_added]["params"] = {
            "metric": metric,
            "cutoff": cutoff,
            "dual_tcr": dual_tcr,
            "receptor_arms": receptor_arms,
        }
        adata.uns[key_added]["connectivities"] = ad.connectivities
        adata.uns[key_added]["distances"] = ad.dist
