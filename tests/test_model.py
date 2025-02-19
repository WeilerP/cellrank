import copy
import io
import itertools
import pathlib
import pickle

import pytest
from _helpers import assert_models_equal, create_model, gamr_skip
from pygam import ExpectileGAM

import numpy as np
import scipy.stats as st
from sklearn.svm import SVR

from anndata import AnnData

from cellrank._utils import Lineage
from cellrank._utils._key import Key
from cellrank.models import GAM, GAMR, FittedModel, SKLearnModel
from cellrank.models._base_model import FailedModel, UnknownModelError
from cellrank.models._pygam_model import GamDistribution, GamLinkFunction, _gams
from cellrank.models._utils import (
    _OFFSET_KEY,
    NormMode,
    _extract_data,
    _get_knotlocs,
    _get_offset,
    _rankdata,
)


class TestModel:
    def test_initialize(self, adata: AnnData):
        model = create_model(adata)

        assert isinstance(model.model, SVR)

    def test_prepare_invalid_gene(self, adata_cflare):
        model = create_model(adata_cflare)
        with pytest.raises(KeyError, match=r"Fatal model"):
            model.prepare("foo", "0", "latent_time")

    def test_prepare_invalid_lineage(self, adata_cflare):
        model = create_model(adata_cflare)
        with pytest.raises(KeyError, match=r"Fatal model"):
            model.prepare(adata_cflare.var_names[0], "foo", "latent_time")

    def test_prepare_invalid_data_key(self, adata_cflare):
        model = create_model(adata_cflare)
        with pytest.raises(KeyError, match=r"Fatal model"):
            model.prepare(adata_cflare.var_names[0], "0", "latent_time", data_key="foo")

    def test_prepare_invalid_time_key(self, adata_cflare):
        model = create_model(adata_cflare)
        with pytest.raises(KeyError, match=r"Fatal model"):
            model.prepare(adata_cflare.var_names[0], "0", "foo")

    def test_prepare_invalid_time_range(self, adata_cflare):
        model = create_model(adata_cflare)
        with pytest.raises(ValueError, match=r"Fatal model"):
            model.prepare(adata_cflare.var_names[0], "0", "latent_time", time_range=(0, 1, 2))

    def test_prepare_normal_run(self, adata_cflare):
        model = create_model(adata_cflare)
        model = model.prepare(adata_cflare.var_names[0], "0", "latent_time")

        assert isinstance(model.x, np.ndarray)
        assert isinstance(model.w, np.ndarray)
        assert isinstance(model.y, np.ndarray)
        assert isinstance(model.x_test, np.ndarray)
        assert len(model.x_test) == 200
        assert model.y_test is None
        assert model.conf_int is None

    def test_prepare_n_test_points(self, adata_cflare):
        model = create_model(adata_cflare)
        model = model.prepare(adata_cflare.var_names[0], "0", "latent_time", n_test_points=300)

        assert len(model.x_test) == 300

    def test_predict(self, adata_cflare):
        model = create_model(adata_cflare)
        model = model.prepare(adata_cflare.var_names[0], "0", "latent_time").fit()
        y_hat = model.predict()

        assert isinstance(model.y_test, np.ndarray)
        assert len(model.x_test) == len(model.y_test)
        assert y_hat is model.y_test

        assert model.conf_int is None

    def test_confidence_interval(self, adata_cflare):
        model = create_model(adata_cflare)
        model = model.prepare(adata_cflare.var_names[0], "0", "latent_time").fit()
        _ = model.predict()
        ci = model.confidence_interval()

        assert isinstance(model.conf_int, np.ndarray)
        assert len(model.y_test) == len(model.conf_int)
        assert ci is model.conf_int

    def test_model_1_lineage(self, adata_cflare):
        adata_cflare.obsm[Key.obsm.fate_probs(False)] = Lineage(np.ones((adata_cflare.n_obs, 1)), names=["foo"])
        model = create_model(adata_cflare)
        model = model.prepare(adata_cflare.var_names[0], "foo", "latent_time", n_test_points=100).fit()
        _ = model.predict()

        assert model.x_test.shape == (100, 1)
        xtest, xall = model.x_test, model.x_all
        np.testing.assert_allclose(np.r_[xtest[0], xtest[-1]], np.r_[np.min(xall), np.max(xall)])

    def test_prepare_resets_fields(self, adata_cflare: AnnData):
        g = GAM(adata_cflare)

        _ = g.prepare(adata_cflare.var_names[0], "0", "latent_time").fit()
        _ = g.predict()
        _ = g.confidence_interval()

        _ = g.prepare(adata_cflare.var_names[1], "0", "latent_time").fit()
        assert isinstance(g.x_test, np.ndarray)
        assert g.y_test is None
        assert g.x_hat is None
        assert g.y_hat is None
        assert g.conf_int is None


class TestUtils:
    def test_extract_data_raw_none(self, adata: AnnData):
        adata = AnnData(adata.X, raw=None)
        with pytest.raises(ValueError, match=r".* is None"):
            _ = _extract_data(adata, use_raw=True)

    def test_extract_data_invalid_layer(self, adata: AnnData):
        with pytest.raises(KeyError, match=r"Layer .* not found"):
            _extract_data(adata, layer="foo", use_raw=False)

    def test_extract_data_normal_run(self, adata: AnnData):
        X = _extract_data(adata, use_raw=False)

        assert X is adata.X

    def test_extract_data_normal_run_layer(self, adata: AnnData):
        ms = _extract_data(adata, layer="Ms", use_raw=False)

        assert ms is adata.layers["Ms"]

    def test_extract_data_normal_run_raw(self, adata: AnnData):
        raw = _extract_data(adata, use_raw=True, layer="Ms")

        assert raw is adata.raw.X

    def test_rank_data_dummy_array(self):
        x = np.ones((100,))

        np.testing.assert_array_equal(_rankdata(x), st.rankdata(x))

    def test_rank_data_empty(self):
        x = np.empty(shape=(0,))

        np.testing.assert_array_equal(_rankdata(x), st.rankdata(x))

    @pytest.mark.parametrize("method", ["average", "min", "max", "dense", "ordinal"])
    def test_rank_data(self, method: str):
        rng = np.random.default_rng(42)
        x = rng.normal(size=(10,))

        np.testing.assert_array_equal(_rankdata(x), st.rankdata(x))

    def test_rank_data_invalid_method(self):
        with pytest.raises(AssertionError, match=r"Invalid ranking method"):
            _rankdata(np.empty((10,)), method="foobar")

    def test_get_knots_invalid_n_knots(self):
        with pytest.raises(ValueError, match=r".* to be positive"):
            _get_knotlocs([0, 1, 2], 0)

    def test_get_knots_non_finite_values(self):
        x = np.array([0, 1, 2, 3], dtype=np.float64)
        x[-1] = np.inf
        with pytest.raises(ValueError, match=r".* are finite"):
            _get_knotlocs(x, 1)

    def test_get_knots_wrong_shape(self):
        with pytest.raises(ValueError, match=".* dimension"):
            _get_knotlocs(np.array([0, 1, 2, 3]).reshape((2, 2)), 1)

    def test_get_knots_only_same_value(self):
        with pytest.raises(ValueError, match=r".* are the same"):
            _get_knotlocs(np.array([42] * 10), 1)

    def test_get_knots_empty_pseudotime(self):
        with pytest.raises(ValueError, match=r".* are the same"):
            _get_knotlocs(np.array([]), 2)

    def test_get_knots_uniform(self):
        expected = np.linspace(0, 5, 3, endpoint=True)
        actual = _get_knotlocs(np.array([3, 5, 4, 0]), 3, uniform=True)

        np.testing.assert_array_equal(actual, expected)

    def test_get_knots_uniform_1_knot(self):
        actual = _get_knotlocs(np.array([3, 5, 4, 0]), 1, uniform=True)

        np.testing.assert_array_equal(actual, [5])

    def test_get_knots_1_knot(self):
        actual = _get_knotlocs(np.array([3, 5, 4, 0]), 1, uniform=False)

        np.testing.assert_array_equal(actual, [5])

    def test_get_knots_2d(self):
        expected = np.linspace(0, 5, 3, endpoint=True)
        actual = _get_knotlocs(np.array([3, 5, 4, 0]).reshape((-1, 1)), 3, uniform=True)

        assert actual.ndim == 1
        np.testing.assert_array_equal(actual, expected)

    @pytest.mark.parametrize(("seed", "n_knots"), zip(range(10), range(2, 11)))
    def test_get_knots_unique(self, seed: int, n_knots: int):
        rng = np.random.default_rng(seed)
        x = rng.normal(size=(100,))
        actual = _get_knotlocs(x, n_knots=n_knots)

        assert actual.shape == (n_knots,)
        np.testing.assert_array_equal(actual, np.sort(actual))
        assert len(np.unique(actual)) == len(actual), actual
        assert (np.min(actual), np.max(actual)) == (np.min(x), np.max(x))

    def test_get_knots_heavy_tail(self):
        x = np.array([0] * 30 + list(np.linspace(0.1, 0.9, 30)) + [1] * 30)
        expected = np.array(
            [
                0.0,
                0.02222222,
                0.04444444,
                0.06666667,
                0.36360153,
                0.63639847,
                0.93333333,
                0.95555556,
                0.97777778,
                1.0,
            ]
        )

        actual = _get_knotlocs(x, 10, uniform=False)

        np.testing.assert_almost_equal(actual, expected)

    @pytest.mark.parametrize(("method", "seed"), zip(list(NormMode), range(len(list(NormMode)))))
    def test_get_offset(self, method: str, seed: int):
        rng = np.random.default_rng(seed)
        x = rng.normal(size=(100, 50))

        offset = _get_offset(x, method=method, ref_ix=0)

        assert isinstance(offset, np.ndarray)
        assert offset.shape == (100,)
        assert np.all(np.isfinite(offset))

    def test_get_offset_degenerate_case(self):
        x = np.zeros((100, 2))

        offset = _get_offset(x, ref_ix=0)

        assert isinstance(offset, np.ndarray)
        np.testing.assert_array_equal(offset, np.ones((100,)))

    def test_get_offset_writing_to_adata(self, adata: AnnData):
        offset = _get_offset(adata, use_raw=False, ref_ix=0)

        assert _OFFSET_KEY in adata.obs
        np.testing.assert_array_equal(offset, adata.obs[_OFFSET_KEY].values)

    def test_get_offset_use_raw(self, adata: AnnData):
        offset = _get_offset(adata, use_raw=False, recompute=True, ref_ix=0)
        offset_raw = _get_offset(adata, use_raw=True, recompute=True, ref_ix=0)

        assert offset.shape == offset_raw.shape == (adata.n_obs,)
        assert not np.all(np.isclose(offset, offset_raw))

    def test_offset_automatic_ref_ix(self, adata: AnnData):
        offset = _get_offset(adata, ref_ix=None)

        assert offset.shape == (adata.n_obs,)
        assert np.all(np.isfinite(offset))


@gamr_skip
class TestGAMR:
    def test_invalid_n_knots(self, adata: AnnData):
        with pytest.raises(ValueError, match=r".* to be positive"):
            _ = GAMR(adata, n_knots=0)

    def test_invalid_smoothing_penalty(self, adata: AnnData):
        with pytest.raises(ValueError, match=r".* to be non-negative"):
            _ = GAMR(adata, smoothing_penalty=-0.001)

    def test_invalid_knotlocs(self, adata: AnnData):
        with pytest.raises(ValueError, match=r"Invalid option"):
            _ = GAMR(adata, knotlocs="foobar")

    def test_density_knotlocs(self, adata_cflare: AnnData):
        g = GAMR(adata_cflare, knotlocs="density")
        g.prepare(adata_cflare.var_names[0], "0", "latent_time", n_test_points=300).fit()
        g.predict(level=0.95)

        assert g.y_test.shape == (300,)
        assert g.conf_int.shape == (300, 2)

    def test_normal_initialization(self, adata_cflare: AnnData):
        m = GAMR(adata_cflare)

        assert not m.prepared
        assert m._lineage is None
        assert m._gene is None
        assert m._offset is None

    def test_negative_binomial_invalid_offset_str(self, adata_cflare: AnnData):
        with pytest.raises(ValueError, match=r"Only value .* is allowed"):
            GAMR(adata_cflare, offset="foobar", distribution="nb")

    def test_negative_binomial_invalid_offset_shape(self, adata_cflare: AnnData):
        with pytest.raises(ValueError, match=r"Expected offset to be of shape"):
            GAMR(
                adata_cflare,
                offset=np.empty(
                    adata_cflare.n_obs + 1,
                ),
                distribution="nb",
            )

    def test_negative_binomial_offset_automatic(self, adata_cflare: AnnData):
        assert _OFFSET_KEY not in adata_cflare.obs
        g = GAMR(adata_cflare, offset="default", distribution="nb")

        assert _OFFSET_KEY in adata_cflare.obs
        np.testing.assert_array_equal(adata_cflare.obs[_OFFSET_KEY].values, g._offset)
        assert g._offset.shape == (adata_cflare.n_obs,)
        assert "offset(offset)" in g._formula

    def test_negative_binomial_offset_ignored_if_not_nb(self, adata_cflare: AnnData):
        g = GAMR(adata_cflare, offset="default", distribution="gaussian")

        assert _OFFSET_KEY not in adata_cflare.obs
        assert g._offset is None

    def test_manually_call_conf_int_not_in_predict(self, adata_cflare: AnnData):
        g = GAMR(adata_cflare).prepare(adata_cflare.var_names[0], "1", "latent_time").fit()
        g.predict(level=None)
        assert g.conf_int is None

        ci_95 = g.confidence_interval(level=0.95)
        np.testing.assert_array_equal(g.conf_int, ci_95)

        ci_100 = g.confidence_interval(level=1)
        np.testing.assert_array_equal(g.conf_int, ci_100)

        assert not np.allclose(ci_95, ci_100)

    def test_sharing_library(self, gamr_model: GAMR):
        actual = gamr_model.copy()

        assert actual._lib_name == gamr_model._lib_name
        assert actual._lib is gamr_model._lib

    def test_shallow_copy(self, gamr_model: GAMR):
        assert_models_equal(gamr_model, copy.copy(gamr_model), deepcopy=False)

    def test_deep_copy(self, gamr_model: GAMR):
        assert_models_equal(gamr_model, copy.deepcopy(gamr_model), deepcopy=True)

    def test_pickling(self, gamr_model: GAMR):
        fp = io.BytesIO()

        pickle.dump(gamr_model, fp)
        fp.flush()
        fp.seek(0)
        actual_model = pickle.load(fp)

        assert_models_equal(gamr_model, actual_model, pickled=True)


class TestSKLearnModel:
    def test_wrong_model_type(self, adata_cflare: AnnData):
        model = create_model(adata_cflare)
        with pytest.raises(TypeError, match=r"Expected model to be of type"):
            SKLearnModel(adata_cflare, model)

    def test_svr_correct_no_weights(self, adata_cflare: AnnData):
        model = (
            SKLearnModel(adata_cflare, SVR(), weight_name="")
            .prepare(adata_cflare.var_names[0], "0", "latent_time")
            .fit()
        )
        model_w = SKLearnModel(adata_cflare, SVR()).prepare(adata_cflare.var_names[0], "0", "latent_time").fit()

        assert model._weight_name == ""
        assert model_w._weight_name == "sample_weight"

        assert not np.allclose(model.predict(), model_w.predict())

    def test_svr_invalid_weight_name(self, adata_cflare: AnnData):
        with pytest.raises(ValueError, match=r"Unable to detect"):
            SKLearnModel(adata_cflare, SVR(), weight_name="foobar")

    def test_svr_invalid_weight_name_no_raise_fit(self, adata_cflare: AnnData):
        model = SKLearnModel(adata_cflare, SVR(), weight_name="w", ignore_raise=True).prepare(
            adata_cflare.var_names[0], "0", "latent_time"
        )

        with pytest.raises(TypeError, match=r"Fatal model"):
            model.fit()

    def test_svr_invalid_weight_name_no_raise(self, adata_cflare: AnnData):
        model = SKLearnModel(adata_cflare, SVR(), weight_name="foobar", ignore_raise=True)

        assert model._weight_name == "foobar"

    def test_svr_correct_weight_name(self, adata_cflare: AnnData):
        model = SKLearnModel(adata_cflare, SVR())

        assert model._weight_name == "sample_weight"


class TestGAM:
    def test_invalid_distribution(self, adata: AnnData):
        with pytest.raises(ValueError, match=r"Invalid option"):
            GAM(adata, distribution="foobar")

    def test_invalid_link_function(self, adata: AnnData):
        with pytest.raises(ValueError, match=r"Invalid option"):
            GAM(adata, link="foob")

    def test_default_grid(self, adata_cflare: AnnData):
        g = GAM(adata_cflare, grid="default")

        g.prepare(adata_cflare.var_names[0], "0", "latent_time")
        g.fit()
        g.predict()
        g.confidence_interval()

        assert g._grid is not None
        assert not isinstance(g._grid, str)
        assert g.y_test is not None
        assert g.conf_int is not None

    def test_custom_grid(self, adata_cflare: AnnData):
        g = GAM(adata_cflare, grid={"lam": [0.1, 1, 10]})

        g.prepare(adata_cflare.var_names[0], "0", "latent_time")
        g.fit()
        g.predict()
        g.confidence_interval()

        assert g._grid is not None
        assert g._grid == {"lam": [0.1, 1, 10]}
        assert g.y_test is not None
        assert g.conf_int is not None

    def test_expectilegam_invalid_expectile(self, adata: AnnData):
        with pytest.raises(ValueError, match=r".* to be in"):
            GAM(adata, expectile=0)
        with pytest.raises(ValueError, match=r".* to be in"):
            GAM(adata, expectile=1)

    def test_expectile_sets_correct_distribution_and_link(self, adata_cflare: AnnData):
        g = GAM(adata_cflare, expectile=0.2)

        g.prepare(adata_cflare.var_names[0], "0", "latent_time")
        g.fit()
        g.predict()
        g.confidence_interval()

        assert isinstance(g.model, ExpectileGAM)
        assert g.y_test is not None
        assert g.conf_int is not None

    @pytest.mark.parametrize(("dist", "link"), itertools.product(list(GamDistribution), list(GamLinkFunction)))
    def test_dist_link_combinations(self, adata_cflare: AnnData, dist: GamDistribution, link: GamLinkFunction):
        g = GAM(adata_cflare, link=link, distribution=dist)

        expected_model_type = _gams[dist, link]

        assert isinstance(g.model, expected_model_type)
        # don't test for link or dist equality (we have normal-gaussian alias, sometimes, they are not strings in pygam)


class TestFailedModel:
    def test_correct_gene_and_lineage(self, pygam_model: GAM):
        fm = FailedModel(pygam_model)

        assert fm.adata is pygam_model.adata
        assert fm.model is pygam_model
        assert fm._gene == pygam_model._gene
        assert fm._lineage == pygam_model._lineage

    def test_do_nothing_no_bulk_fit(self, pygam_model: GAM):
        fm = FailedModel(pygam_model)

        for fn in [
            "prepare",
            "fit",
            "predict",
            "confidence_interval",
            "default_confidence_interval",
            "plot",
        ]:
            with pytest.raises(UnknownModelError, match=r"Fatal model"):
                getattr(fm, fn)()

    def test_do_nothing_bulk_fit(self, pygam_model: GAM):
        pygam_model._is_bulk = True
        fm = FailedModel(pygam_model)
        expected_dict = fm.__dict__.copy()

        for fn in [
            "prepare",
            "fit",
            "predict",
            "confidence_interval",
            "default_confidence_interval",
            "plot",
        ]:
            getattr(fm, fn)()

        assert expected_dict == fm.__dict__

    def test_copy(self, pygam_model):
        fm1 = FailedModel(pygam_model)
        fm2 = fm1.copy()

        assert fm1.model is not fm2.model
        assert fm1.adata is fm2.adata

    def test_reraise(self, pygam_model: GAM):
        fm = FailedModel(pygam_model, exc=ValueError("foobar"))

        with pytest.raises(ValueError, match=r"Fatal model"):
            fm.reraise()

        assert isinstance(fm._exc, ValueError)

    def test_reraise_str(self, pygam_model: GAM):
        fm = FailedModel(pygam_model, exc="foobar")

        with pytest.raises(RuntimeError, match=r"Fatal model"):
            fm.reraise()

        assert isinstance(fm._exc, RuntimeError)

    def test_str_repr(self, pygam_model: GAM):
        expected = f"<FailedModel[origin={str(pygam_model).strip('<>')}]>"
        fm = FailedModel(pygam_model)

        assert str(fm) == expected
        assert repr(fm) == expected


class TestModelsIO:
    def test_shallow_copy_sklearn(self, sklearn_model: SKLearnModel):
        assert_models_equal(sklearn_model, copy.copy(sklearn_model), deepcopy=False)

    def test_deep_copy_sklearn(self, sklearn_model: SKLearnModel):
        assert_models_equal(sklearn_model, copy.deepcopy(sklearn_model), deepcopy=True)

    def test_pickling_sklearn(self, sklearn_model: SKLearnModel):
        fp = io.BytesIO()

        pickle.dump(sklearn_model, fp)
        fp.flush()
        fp.seek(0)
        actual_model = pickle.load(fp)

        assert_models_equal(sklearn_model, actual_model, pickled=True)

    def test_shallow_copy_pygam(self, pygam_model: GAM):
        assert_models_equal(pygam_model, copy.copy(pygam_model), deepcopy=False)

    def test_deep_copy_pygam(self, pygam_model: GAM):
        assert_models_equal(pygam_model, copy.deepcopy(pygam_model), deepcopy=True)

    def test_pickling_pygam(self, pygam_model: GAM):
        fp = io.BytesIO()

        pickle.dump(pygam_model, fp)
        fp.flush()
        fp.seek(0)
        actual_model = pickle.load(fp)

        assert_models_equal(pygam_model, actual_model, pickled=True)

    @pytest.mark.parametrize("copy", [False, True])
    @pytest.mark.parametrize("write_adata", [False, True])
    def test_read_write(self, sklearn_model: SKLearnModel, tmpdir, write_adata: bool, copy: bool):
        path = pathlib.Path(tmpdir) / "model.pickle"
        sklearn_model.write(path, write_adata=write_adata)

        if write_adata:
            model = SKLearnModel.read(path)
            assert model.adata is not None
        else:
            with open(path, "rb") as fin:
                model: SKLearnModel = pickle.load(fin)
                assert model.adata is None
                assert model.shape == (sklearn_model.adata.n_obs,)
            model = SKLearnModel.read(path, adata=sklearn_model.adata, copy=copy)
            if copy:
                assert model.adata is not sklearn_model.adata
            else:
                assert model.adata is sklearn_model.adata
            model.adata = model.adata.copy()
        assert_models_equal(sklearn_model, model, pickled=True)


class TestFittedModel:
    def test_wrong_xt_yt_shape(self):
        with pytest.raises(ValueError, match=r".* to be of shape"):
            FittedModel(np.array([1]), np.array([2, 3]))

    def test_wrong_xt_dum(self):
        with pytest.raises(ValueError, match=r".* to be of shape"):
            FittedModel(np.array([[0, 1], [1, 2]]), np.array([2, 3]))

    def test_wrong_conf_int_dim(self):
        with pytest.raises(ValueError, match=r".* to be of shape"):
            FittedModel(np.array([0, 1]), np.array([2, 3]), conf_int=np.array([4, 5]))

    def test_wrong_conf_int_wrong_shape(self):
        with pytest.raises(ValueError, match=r".* of shape"):
            FittedModel(
                np.array([0, 1]),
                np.array([2, 3]),
                conf_int=np.array([[4, 5], [6, 7], [8, 9]]),
            )

    def test_densify_only_first_axis(self):
        with pytest.raises(ValueError, match=r".* of shape"):
            FittedModel(np.array([[[0, 1]]]), np.array([2, 3]))

    def test_wrong_x_all_shape(self):
        with pytest.raises(ValueError, match=r".* of shape"):
            FittedModel(
                np.array([[0, 1]]),
                np.array([2, 3]),
                x_all=np.array([[4, 5, 6]]),
                y_all=np.array([7, 8]),
            )

    def test_wrong_y_all_shape(self):
        with pytest.raises(ValueError, match=r".* of shape"):
            FittedModel(
                np.array([[0, 1]]),
                np.array([2, 3]),
                x_all=np.array([[4, 5]]),
                y_all=np.array([6, 8, 7]),
            )

    def test_wrong_w_all_shape(self):
        with pytest.raises(ValueError, match=r".* of shape"):
            FittedModel(
                np.array([[0, 1]]),
                np.array([2, 3]),
                x_all=np.array([[4, 5]]),
                y_all=np.array([6, 7]),
                w_all=np.array([8]),
            )

    def test_conf_int_raise_error_missing(self):
        fm = FittedModel([0, 1, 2], [3, 4, 5])
        with pytest.raises(RuntimeError, match=r"No confidence"):
            fm.confidence_interval()

        with pytest.raises(RuntimeError, match=r"No confidence"):
            fm.default_confidence_interval()

    def test_zero_array(self):
        fm = FittedModel(np.array([]), np.array([]))

        np.testing.assert_array_equal(fm.x_test, np.array([[]]).reshape((0, 1)))
        np.testing.assert_array_equal(fm.y_test, [])

        assert fm.conf_int is None
        assert fm.x_all is None
        assert fm.w_all is None
        assert fm.y_all is None

    def test_non_array_input(self):
        fm = FittedModel([0, 1, 2], [3, 4, 5])

        np.testing.assert_array_equal(fm.x_test, [[0], [1], [2]])
        np.testing.assert_array_equal(fm.y_test, [3, 4, 5])

        assert fm.conf_int is None
        assert fm.x_all is None
        assert fm.w_all is None
        assert fm.y_all is None

    def test_wrong_conf_int(self):
        fm = FittedModel(np.array([0, 1]), np.array([2, 3]), conf_int=np.array([[4, 5], [6, 7]]))

        np.testing.assert_array_equal(fm.x_test, [[0], [1]])
        np.testing.assert_array_equal(fm.y_test, [2, 3])
        np.testing.assert_array_equal(fm.conf_int, [[4, 5], [6, 7]])

        assert fm.x_all is None
        assert fm.w_all is None
        assert fm.y_all is None

    def test_only_partial_x_all(self):
        fm = FittedModel(np.array([0, 1]), np.array([2, 3]), x_all=[4, 5], w_all=[6, 7])

        np.testing.assert_array_equal(fm.x_test, [[0], [1]])
        np.testing.assert_array_equal(fm.y_test, [2, 3])

        assert fm.x_all is None
        assert fm.w_all is None
        assert fm.y_all is None

    def test_full_initialization(self):
        fm = FittedModel(
            np.array([0, 1]),
            np.array([2, 3]),
            conf_int=np.array([[4, 5], [6, 7]]),
            x_all=[4, 5],
            y_all=(6, 7),
            w_all=[8, 9],
        )

        assert fm.prepared

        np.testing.assert_array_equal(fm.x_test, [[0], [1]])
        np.testing.assert_array_equal(fm.y_test, [2, 3])
        np.testing.assert_array_equal(fm.conf_int, [[4, 5], [6, 7]])

        np.testing.assert_array_equal(fm.x_all, [[4], [5]])
        np.testing.assert_array_equal(fm.y_all, [[6], [7]])
        np.testing.assert_array_equal(fm.w_all, [8, 9])

    def test_normal_run(self):
        fm = FittedModel(
            np.array([0, 1]),
            np.array([2, 3]),
            conf_int=np.array([[4, 5], [6, 7]]),
            x_all=[4, 5],
            y_all=(6, 7),
            w_all=[8, 9],
        )

        fm = fm.prepare()
        assert fm is fm

        fm = fm.fit()
        assert fm is fm

        np.testing.assert_array_equal(fm.predict(), [2, 3])
        np.testing.assert_array_equal(fm.confidence_interval(), [[4, 5], [6, 7]])
        np.testing.assert_array_equal(fm.default_confidence_interval(), [[4, 5], [6, 7]])

    def test_from_model_wrong_type(self, adata_cflare):
        m = create_model(adata_cflare)
        with pytest.raises(TypeError, match=r".* to be of type"):
            FittedModel.from_model(m.model)

    def test_from_model_not_fitted_model(self, adata_cflare: AnnData):
        m = create_model(adata_cflare).prepare(adata_cflare.var_names[0], "1", "latent_time")
        with pytest.raises(ValueError, match=r".* to be of shape"):
            FittedModel.from_model(m)

    def test_from_model_normal_run(self, adata_cflare: AnnData):
        m = create_model(adata_cflare).prepare(adata_cflare.var_names[0], "1", "latent_time").fit()
        m.predict()
        m.confidence_interval()

        fm = FittedModel.from_model(m)

        assert fm.prepared
        assert fm._gene == m._gene
        assert fm._lineage == m._lineage

        np.testing.assert_array_equal(fm.x_test, m.x_test)
        assert fm.x_test is not m.x_test
        np.testing.assert_array_equal(fm.y_test, m.y_test)
        assert fm.y_test is not m.y_test
        np.testing.assert_array_equal(fm.conf_int, m.conf_int)
        assert fm.conf_int is not m.conf_int

        np.testing.assert_array_equal(fm.x_all, m.x_all)
        assert fm.x_all is not m.x_all
        np.testing.assert_array_equal(fm.y_all, m.y_all)
        assert fm.y_all is not m.y_all
        np.testing.assert_array_equal(fm.w_all, m.w_all)
        assert fm.y_all is not m.w_all

    def test_fitted_copy(self):
        m = FittedModel(
            np.array([0, 1]),
            np.array([2, 3]),
            conf_int=np.array([[4, 5], [6, 7]]),
            x_all=[4, 5],
            y_all=(6, 7),
            w_all=[8, 9],
        )

        fm = m.copy()

        assert isinstance(fm, FittedModel)
        assert fm.prepared
        assert fm._gene == m._gene
        assert fm._lineage == m._lineage

        np.testing.assert_array_equal(fm.x_test, m.x_test)
        assert fm.x_test is not m.x_test
        np.testing.assert_array_equal(fm.y_test, m.y_test)
        assert fm.y_test is not m.y_test
        np.testing.assert_array_equal(fm.conf_int, m.conf_int)
        assert fm.conf_int is not m.conf_int

        np.testing.assert_array_equal(fm.x_all, m.x_all)
        assert fm.x_all is not m.x_all
        np.testing.assert_array_equal(fm.y_all, m.y_all)
        assert fm.y_all is not m.y_all
        np.testing.assert_array_equal(fm.w_all, m.w_all)
        assert fm.y_all is not m.w_all
