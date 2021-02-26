# pylama:ignore=W0611,W0404
import pandas as pd
import numpy.testing as npt
import pandas.testing as pdt
import scirpy as ir
from anndata import AnnData
import numpy as np
from scirpy.util import _is_symmetric
from .fixtures import (
    adata_define_clonotype_clusters_singletons,
    adata_define_clonotypes,
    adata_define_clonotype_clusters,
    adata_clonotype_network,
    adata_clonotype,
)
import random
import pytest

# TODO test distance_keys return values
@pytest.mark.parametrize("key_added", [None, "my_key"])
@pytest.mark.parametrize("inplace", [True, False])
def test_define_clonotype_clusters_return_values(
    adata_define_clonotype_clusters_singletons, key_added, inplace
):
    """Test that key_added and inplace work as expected"""
    adata = adata_define_clonotype_clusters_singletons
    res = ir.tl.define_clonotype_clusters(
        adata,
        receptor_arms="VJ",
        dual_ir="primary_only",
        metric="identity",
        sequence="aa",
        same_v_gene=True,
        inplace=inplace,
        key_added=key_added,
    )  # type: ignore

    clonotype_expected = np.array([0, 1, 2, 3]).astype(str)
    clonotype_size_expected = np.array([1, 1, 1, 1])

    if inplace:
        assert res is None
        if key_added is None:
            npt.assert_equal(adata.obs["cc_aa_identity"], clonotype_expected)
            npt.assert_equal(adata.obs["cc_aa_identity_size"], clonotype_size_expected)
        else:
            npt.assert_equal(adata.obs["my_key"], clonotype_expected)
            npt.assert_equal(adata.obs["my_key_size"], clonotype_size_expected)

    else:
        npt.assert_equal(res[0], clonotype_expected)
        npt.assert_equal(res[1], clonotype_size_expected)


@pytest.mark.parametrize("receptor_arms", ["VJ", "VDJ", "all", "any"])
@pytest.mark.parametrize("dual_ir", ["primary_only", "all", "any"])
def test_define_clonotypes_diagonal_connectivities(
    adata_define_clonotype_clusters_singletons, receptor_arms, dual_ir
):
    """Regression test for #236. Computing the clonotypes when
    no cells are connected in the clonotype neighborhood graph should not fail."""
    clonotype, clonotype_size, _ = ir.tl.define_clonotype_clusters(
        adata_define_clonotype_clusters_singletons,
        receptor_arms=receptor_arms,
        dual_ir=dual_ir,
        metric="identity",
        sequence="aa",
        same_v_gene=True,
        inplace=False,
    )  # type: ignore
    npt.assert_equal(clonotype, np.array([0, 1, 2, 3]).astype(str))
    npt.assert_equal(clonotype_size, np.array([1, 1, 1, 1]))


def test_clonotypes_end_to_end1(adata_define_clonotypes):
    """Test that default parameters of define_clonotypes yields
    clonotypes based on nt-identity."""
    ir.pp.ir_dist(adata_define_clonotypes)
    clonotypes, clonotype_size, _ = ir.tl.define_clonotypes(
        adata_define_clonotypes,
        inplace=False,
        within_group=None,
        receptor_arms="all",
        dual_ir="all",
    )  # type: ignore
    print(clonotypes)
    expected = [0, 0, 1, 2, 3]
    expected_size = [2, 2, 1, 1, 1]
    npt.assert_equal(clonotypes.values, [str(x) for x in expected])
    npt.assert_equal(clonotype_size.values, expected_size)


@pytest.mark.parametrize(
    "receptor_arms,dual_ir,same_v_gene,within_group,expected,expected_size",
    [
        (
            "all",
            "all",
            False,
            None,
            [0, 0, 1, 2, 3, np.nan, 4, 5, 6, 7, 8],
            [2, 2, 1, 1, 1, np.nan, 1, 1, 1, 1, 1],
        ),
        (
            "any",
            "any",
            False,
            None,
            [0, 0, 0, 0, 0, np.nan, 0, 0, 0, 0, 1],
            [9, 9, 9, 9, 9, np.nan, 9, 9, 9, 9, 1],
        ),
        (
            "all",
            "any",
            False,
            None,
            [0, 0, 0, 0, 0, np.nan, 0, 1, 0, 2, 3],
            [7, 7, 7, 7, 7, np.nan, 7, 1, 7, 1, 1],
        ),
        (
            "any",
            "all",
            False,
            None,
            [0, 0, 0, 0, 0, np.nan, 0, 0, 0, 0, 1],
            [9, 9, 9, 9, 9, np.nan, 9, 9, 9, 9, 1],
        ),
        (
            "all",
            "primary_only",
            False,
            None,
            [0, 0, 1, 2, 0, np.nan, 0, 3, 4, 5, 6],
            [4, 4, 1, 1, 4, np.nan, 4, 1, 1, 1, 1],
        ),
        (
            "VDJ",
            "primary_only",
            False,
            None,
            [0, 0, 0, 1, 0, np.nan, 0, 2, 3, 3, 4],
            [5, 5, 5, 1, 5, np.nan, 5, 1, 2, 2, 1],
        ),
        # by receptor type
        (
            "any",
            "any",
            False,
            "receptor_type",
            [0, 0, 0, 1, 1, np.nan, 0, 0, 0, 1, 2],
            [6, 6, 6, 3, 3, np.nan, 6, 6, 6, 3, 1],
        ),
        # different combinations with same_v_gene
        (
            "all",
            "all",
            True,
            None,
            [0, 1, 2, 3, 4, np.nan, 5, 6, 7, 8, 9],
            [1, 1, 1, 1, 1, np.nan, 1, 1, 1, 1, 1],
        ),
        (
            "any",
            "any",
            True,
            None,
            [0, 0, 0, 1, 0, np.nan, 0, 0, 0, 0, 2],
            [8, 8, 8, 1, 8, np.nan, 8, 8, 8, 8, 1],
        ),
        (
            "VDJ",
            "primary_only",
            True,
            None,
            [0, 0, 0, 1, 0, np.nan, 0, 2, 3, 4, 5],
            [5, 5, 5, 1, 5, np.nan, 5, 1, 1, 1, 1],
        ),
        # v gene and receptor type
        (
            "any",
            "any",
            True,
            "receptor_type",
            [0, 0, 0, 1, 2, np.nan, 0, 0, 0, 2, 3],
            [6, 6, 6, 1, 2, np.nan, 6, 6, 6, 2, 1],
        ),
    ],
)
def test_clonotype_clusters_end_to_end(
    adata_define_clonotype_clusters,
    receptor_arms,
    dual_ir,
    same_v_gene,
    within_group,
    expected,
    expected_size,
):
    """Test define_clonotype_clusters with different parameters"""
    ir.pp.ir_dist(
        adata_define_clonotype_clusters,
        cutoff=0,
        sequence="aa",
    )
    clonotypes, clonotype_size, _ = ir.tl.define_clonotype_clusters(
        adata_define_clonotype_clusters,
        inplace=False,
        within_group=within_group,
        receptor_arms=receptor_arms,
        dual_ir=dual_ir,
        same_v_gene=same_v_gene,
    )  # type: ignore
    print(clonotypes)
    npt.assert_equal(
        list(clonotypes.values), [str(x) if not np.isnan(x) else x for x in expected]
    )
    npt.assert_almost_equal(clonotype_size.values, expected_size)


def test_clonotype_network(adata_conn):
    ir.tl.define_clonotype_clusters(
        adata_conn,
        sequence="aa",
        metric="alignment",
        partitions="connected",
        within_group=None,
    )
    random.seed(42)
    coords = ir.tl.clonotype_network(
        adata_conn,
        sequence="aa",
        metric="alignment",
        min_size=1,
        layout="fr",
        inplace=False,
    )
    npt.assert_almost_equal(
        coords,
        np.array(
            [
                [5.147361, 3.1383265],
                [3.4346971, 4.2259229],
                [4.0405687, 3.4865629],
                [5.2082453, 5.1293543],
            ]
        ),
    )

    random.seed(42)
    ir.tl.clonotype_network(
        adata_conn,
        sequence="aa",
        metric="alignment",
        min_size=2,
        layout="components",
        inplace=True,
        key_added="ctn",
    )
    coords = adata_conn.obsm["X_ctn"]
    npt.assert_almost_equal(
        coords,
        np.array(
            [[98.0, 1.0], [1.0, 98.0], [49.5107979, 49.4911286], [np.nan, np.nan]]
        ),
    )

    with pytest.raises(ValueError):
        ir.tl.clonotype_network(adata_conn[[1, 3], :])


def test_clonotype_network_igraph(adata_clonotype_network):
    g, lo = ir.tl.clonotype_network_igraph(adata_clonotype_network)
    assert g.vcount() == 3
    npt.assert_almost_equal(
        np.array(lo.coords),
        np.array(
            [
                [2.41359095, 0.23412465],
                [1.61680611, 0.80266963],
                [3.06104282, 2.14395562],
            ]
        ),
    )


def test_clonotype_convergence(adata_clonotype):
    res = ir.tl.clonotype_convergence(
        adata_clonotype,
        key_coarse="clonotype_cluster",
        key_fine="clonotype",
        inplace=False,
    )
    ir.tl.clonotype_convergence(
        adata_clonotype,
        key_coarse="clonotype_cluster",
        key_fine="clonotype",
        inplace=True,
        key_added="is_convergent_",
    )
    pdt.assert_extension_array_equal(res, adata_clonotype.obs["is_convergent_"].values)
    pdt.assert_extension_array_equal(
        res,
        pd.Categorical(
            ["not convergent"] * 5 + ["convergent"] * 2 + ["not convergent"] * 2,
            categories=["convergent", "not convergent"],
        ),
    )

    res = ir.tl.clonotype_convergence(
        adata_clonotype,
        key_fine="clonotype_cluster",
        key_coarse="clonotype",
        inplace=False,
    )
    pdt.assert_extension_array_equal(
        res,
        pd.Categorical(
            ["not convergent"] * 9,
            categories=["convergent", "not convergent"],
        ),
    )
