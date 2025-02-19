import pathlib
import warnings
from typing import Optional

import pytest
from _helpers import create_model

import numpy as np
from numba.core.errors import NumbaPerformanceWarning

import matplotlib

import scanpy as sc
from anndata import AnnData

import cellrank as cr
from cellrank.estimators import CFLARE, GPCCA
from cellrank.kernels import ConnectivityKernel, VelocityKernel
from cellrank.models import GAM, GAMR, SKLearnModel

_adata_small = sc.read("tests/_ground_truth_adatas/adata_50.h5ad")
_adata_medium = sc.read("tests/_ground_truth_adatas/adata_100.h5ad")
_adata_large = sc.read("tests/_ground_truth_adatas/adata_200.h5ad")


def pytest_sessionstart(session: pytest.Session) -> None:
    matplotlib.use("Agg")
    matplotlib.rcParams["figure.max_open_warning"] = 0
    np.random.seed(42)  # noqa: NPY002

    # https://github.com/theislab/cellrank/issues/683
    warnings.simplefilter("ignore", NumbaPerformanceWarning)


# removes overly verbose and useless logging errors for rpy2
# see: https://github.com/pytest-dev/pytest/issues/5502#issuecomment-647157873
def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    import logging

    loggers = [logging.getLogger()] + list(logging.Logger.manager.loggerDict.values())
    for logger in loggers:
        handlers = getattr(logger, "handlers", [])
        for handler in handlers:
            logger.removeHandler(handler)


def _create_cflare(*, backward: bool = False) -> tuple[AnnData, CFLARE]:
    adata = _adata_medium.copy()

    sc.tl.paga(adata, groups="clusters")

    vk = VelocityKernel(adata, backward=backward).compute_transition_matrix(softmax_scale=4)
    ck = ConnectivityKernel(adata).compute_transition_matrix()
    final_kernel = 0.8 * vk + 0.2 * ck

    mc = CFLARE(final_kernel)
    final_kernel.write_to_adata()

    mc.compute_eigendecomposition()
    mc.predict(use=2, method="kmeans")
    mc.compute_fate_probabilities(use_petsc=False)
    mc.compute_lineage_drivers(cluster_key="clusters", use_raw=False)

    assert adata is mc.adata
    np.testing.assert_allclose(mc.fate_probabilities.X.sum(1), 1.0, rtol=1e-6)

    return adata, mc


def _create_gpcca(*, backward: bool = False) -> tuple[AnnData, GPCCA]:
    adata = _adata_medium.copy()

    sc.tl.paga(adata, groups="clusters")

    vk = VelocityKernel(adata, backward=backward).compute_transition_matrix(softmax_scale=4)
    ck = ConnectivityKernel(adata).compute_transition_matrix()
    final_kernel = 0.8 * vk + 0.2 * ck

    mc = GPCCA(final_kernel)
    final_kernel.write_to_adata()

    mc.compute_eigendecomposition()
    mc.compute_schur(method="krylov")
    mc.compute_macrostates(n_states=2)
    mc.set_terminal_states()
    mc.compute_fate_probabilities(use_petsc=False)
    mc.compute_lineage_drivers(cluster_key="clusters", use_raw=False)

    assert adata is mc.adata
    np.testing.assert_allclose(mc.fate_probabilities.X.sum(1), 1.0, rtol=1e-6)

    return adata, mc


def _create_gamr_model(_adata: AnnData) -> Optional[GAMR]:
    try:
        m = GAMR(_adata)
        m.prepare(_adata.var_names[0], "0", "latent_time").fit()
        m.predict(level=0.95)
        return m
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture
def adata() -> AnnData:
    return _adata_small.copy()


@pytest.fixture
def adata_large() -> AnnData:
    return _adata_large.copy()


@pytest.fixture
def adata_cflare_fwd(
    adata_cflare=_create_cflare(backward=False),  # noqa: B008
) -> tuple[AnnData, CFLARE]:
    adata, cflare = adata_cflare
    return adata.copy(), cflare


@pytest.fixture
def adata_gpcca_fwd(adata_gpcca=_create_gpcca(backward=False)) -> tuple[AnnData, GPCCA]:  # noqa: B008
    adata, gpcca = adata_gpcca
    return adata.copy(), gpcca


@pytest.fixture
def adata_gpcca_bwd(adata_gpcca=_create_gpcca(backward=True)) -> tuple[AnnData, GPCCA]:  # noqa: B008
    adata, gpcca = adata_gpcca
    return adata.copy(), gpcca


@pytest.fixture
def adata_cflare(adata_cflare=_create_cflare(backward=False)) -> AnnData:  # noqa: B008
    return adata_cflare[0].copy()


@pytest.fixture
def g(adata_gpcca=_create_gpcca(backward=False)) -> tuple[AnnData, GPCCA]:  # noqa: B008
    return adata_gpcca[1].copy()


@pytest.fixture(scope="session")
def adata_gamr(adata_cflare=_create_cflare(backward=False)) -> AnnData:  # noqa: B008
    return adata_cflare[0].copy()


@pytest.fixture
def gamr_model(adata_gamr: AnnData, tmp_path_factory: pathlib.Path) -> Optional[GAMR]:
    return _create_gamr_model(adata_gamr)


@pytest.fixture
def pygam_model(adata_cflare: AnnData) -> GAM:
    m = GAM(adata_cflare)
    m.prepare(adata_cflare.var_names[0], "0", "latent_time").fit()
    m.predict()
    m.confidence_interval()

    return m


@pytest.fixture
def sklearn_model(adata_cflare: AnnData) -> SKLearnModel:
    m = create_model(adata_cflare)
    assert isinstance(m, SKLearnModel), m

    m.prepare(adata_cflare.var_names[0], "0", "latent_time").fit()
    m.predict()
    m.confidence_interval()

    return m


@pytest.fixture
def lineage():
    x = cr._utils.Lineage(
        np.array(
            [
                [1.23459664e-01, 1.29965675e-01, 1.92828002e-01, 9.39402664e-01],
                [1.05635239e00, 4.45833459e-01, 2.29080759e00, 1.90132652e00],
                [6.77880737e-02, 4.97556864e-02, 1.18428661e00, 2.02318999e-01],
                [4.87500398e-01, 1.00657498e00, 2.20834882e-02, 5.03008905e-01],
                [6.27190917e00, 7.27864781e00, 1.03978903e00, 1.55903460e01],
                [3.85149269e-01, 3.54765380e-01, 1.77871487e-01, 8.22138648e-02],
                [7.06618729e00, 1.33133671e01, 1.44904591e00, 5.79813391e00],
                [8.18005744e-02, 5.36844933e-01, 1.86646162e00, 2.41141727e00],
                [1.44892035e-01, 2.34036215e-01, 6.32392890e-01, 1.13211403e-02],
                [2.44926466e-01, 2.50293183e-01, 1.77540208e-01, 3.27240144e-01],
            ]
        ),
        names=["foo", "bar", "baz", "quux"],
    )
    return x / x.sum(1)


@pytest.fixture
def kernel(adata_large: AnnData):
    vk = VelocityKernel(adata_large).compute_transition_matrix(softmax_scale=4)
    ck = ConnectivityKernel(adata_large).compute_transition_matrix()
    return (0.8 * vk + 0.2 * ck).compute_transition_matrix()


@pytest.fixture(scope="session")
def test_matrix_1() -> np.ndarray:
    """
    Parameters
    ----------

    Returns
    -------
    :class:`numpy.ndarray`
        Row-normalized transition matrix. This matrix is
        - connected
        - irreducible
        - not reversible
    """

    # fmt: off
    return np.array([
        # 0.   1.   2.   3.   4.   5.   6.   7.   8.   9.   10.  11.
        [0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 0
        [0.2, 0.0, 0.6, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 1
        [0.6, 0.2, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 2

        [0.0, 0.05, 0.05, 0.0, 0.6, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 3
        [0.0, 0.0, 0.0, 0.25, 0.0, 0.25, 0.4, 0.0, 0.0, 0.1, 0.0, 0.0],  # 4
        [0.0, 0.0, 0.0, 0.25, 0.25, 0.0, 0.1, 0.0, 0.0, 0.4, 0.0, 0.0],  # 5

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.7, 0.2, 0.0, 0.0, 0.0],  # 6
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8, 0.0, 0.0, 0.0],  # 7
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0],  # 8

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0, 0.7, 0.2],  # 9
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8],  # 10
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0],  # 11
    ])
    # fmt: on


@pytest.fixture(scope="session")
def test_matrix_2() -> np.ndarray:
    """
    Parameters
    ----------

    Returns
    -------
    :class:`numpy.ndarray`
        Row-normalized transition matrix. This matrix is
        - connected
        - not irreducible (1 recurrent, 1 transient class)
        - not reversible
    """
    # fmt: off
    return np.array([
        # 0.   1.   2.   3.   4.   5.   6.   7.   8.   9.   10.  11.  12.  13.
        [0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 0
        [0.2, 0.0, 0.6, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 1
        [0.6, 0.2, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 2

        [0.0, 0.05, 0.05, 0.0, 0.6, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 3
        [0.0, 0.0, 0.0, 0.25, 0.0, 0.25, 0.4, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0, 0.0],  # 4
        [0.0, 0.0, 0.0, 0.25, 0.25, 0.0, 0.1, 0.0, 0.0, 0.3, 0.0, 0.0, 0.1, 0.0],  # 5

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.7, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0],  # 6
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0],  # 7
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 8

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0, 0.7, 0.2, 0.0, 0.0],  # 9
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8, 0.0, 0.0],  # 10
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 0.0],  # 11

        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6, 0.4],  # 12
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3, 0.7],  # 13
    ])
    # fmt: on


@pytest.fixture(scope="session")
def test_matrix_3() -> np.ndarray:
    """
    Parameters
    ----------

    Returns
    -------
    :class:`numpy.ndarray`
        Row-normalized transition matrix. This matrix is
        - not connected
        - not irreducible (2 recurrent classes)
        - not reversible
    """

    # fmt: off
    return np.array([
        # 0.   1.   2.   3.   4.   5.   6.   7.   8.   9.   10.  11.  12.  13.
        [0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 0
        [0.2, 0.0, 0.6, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 1
        [0.6, 0.2, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 2

        [0.0, 0.05, 0.05, 0.0, 0.6, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 3
        [0.0, 0.0, 0.0, 0.25, 0.0, 0.25, 0.4, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0, 0.0],  # 4
        [0.0, 0.0, 0.0, 0.25, 0.25, 0.0, 0.1, 0.0, 0.0, 0.4, 0.0, 0.0, 0.0, 0.0],  # 5

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.7, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0],  # 6
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0],  # 7
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 8

        [0.0, 0.0, 0.0, 0.0, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0, 0.7, 0.2, 0.0, 0.0],  # 9
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.8, 0.0, 0.0],  # 10
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 0.0],  # 11

        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6, 0.4],  # 12
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3, 0.7],  # 13
    ])
    # fmt: on


@pytest.fixture(scope="session")
def test_matrix_4() -> np.ndarray:
    """
    Parameters
    ----------

    Returns
    -------
    :class:`numpy.ndarray`
        Symmetric matrix. Not a transition matrix.
    """

    # fmt: off
    return np.array(
        [
            # 0.   1.   2.   3.
            [0.0, 0.8, 0.2, 0.0],  # 0
            [0.8, 0.0, 0.6, 0.2],  # 1
            [0.2, 0.6, 0.0, 0.2],  # 2
            [0.0, 0.2, 0.2, 0.0],  # 3
        ]
    )
    # fmt: on
