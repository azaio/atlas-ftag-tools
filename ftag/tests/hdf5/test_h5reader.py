from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

import h5py
import numpy as np
import pytest
from numpy.lib.recfunctions import unstructured_to_structured as u2s

from ftag import get_mock_file
from ftag.cuts import Cuts
from ftag.hdf5.h5reader import H5Reader, H5SingleReader
from ftag.sample import Sample
from ftag.transform import Transform

np.random.seed(42)


# parameterise the test
@pytest.mark.parametrize("num", [1, 2, 3])
@pytest.mark.parametrize("length", [200, 301])
@pytest.mark.parametrize("equal_jets", [True, False])
def test_H5Reader(num, length, equal_jets):
    # calculate all possible effective batch sizes, from single file batch sizes and remainders
    batch_size = 100
    effective_bs_file = batch_size // num
    remainders = [(length * n) % effective_bs_file for n in range(num + 1)]
    effective_bs_options = [effective_bs_file * n + r for n in range(num + 1) for r in remainders]
    effective_bs_options = [x for x in effective_bs_options if x <= batch_size][1:]

    # create test files (of different lengths)
    tmpdirs = []
    file_lengths = []
    for i in range(num):
        fname = NamedTemporaryFile(suffix=".h5", dir=mkdtemp()).name
        tmpdirs.append(Path(fname).parent)
        jets_length = length * (i + 1)
        file_lengths.append(jets_length)
        with h5py.File(fname, "w") as f:
            data = i * np.ones((jets_length, 2))
            data = u2s(data, dtype=[("x", "f4"), ("y", "f4")])
            f.create_dataset("jets", data=data)

            data = i * np.ones((jets_length, 40, 2))
            data = u2s(data, dtype=[("a", "f4"), ("b", "f4")])
            f.create_dataset("tracks", data=data)

    # create a multi-path sample
    sample = Sample([f"{x}/*.h5" for x in tmpdirs], name="test")

    # test reading from multiple paths
    reader = H5Reader(sample.path, batch_size=batch_size, equal_jets=equal_jets)
    # dynamically compute valid total batch sizes (sum over per-file batch_sizes)
    total_jets = sum(file_lengths)
    weights = [n / total_jets for n in file_lengths]
    per_file_bs = [int(batch_size * w) for w in weights]

    # all combinations of n * per_file_bs[i] + remainders (as original test tried to capture)
    effective_bs_options = []
    for i in range(num + 1):
        for r in range(batch_size):
            total_bs = sum(per_file_bs)
            val = i * total_bs + r
            if 0 < val <= batch_size:
                effective_bs_options.append(val)
    effective_bs_options = list(set(effective_bs_options))  # remove duplicates

    assert reader.num_jets == total_jets

    variables = {"jets": ["x", "y"], "tracks": None}
    for data in reader.stream(variables=variables):
        assert "jets" in data
        assert data["jets"].shape in [(effective_bs,) for effective_bs in effective_bs_options]
        assert len(data["jets"].dtype.names) == 2
        assert "tracks" in data
        assert data["tracks"].shape in [(effective_bs, 40) for effective_bs in effective_bs_options]
        assert len(data["tracks"].dtype.names) == 2
        if equal_jets:  # if equal_jets is off, batches won't necessarily have data from all files
            assert (np.unique(data["jets"]["x"]) == np.array(list(range(num)))).all()

        # check that the tracks are correctly matched to the jets
        for i in range(num):
            trk = (data["tracks"]["a"] == i).all(-1)
            jet = data["jets"]["x"] == i
            assert (jet == trk).all()

        if num > 1:
            corr = np.corrcoef(data["jets"]["x"], data["tracks"]["a"][:, 0])
            np.testing.assert_allclose(corr, 1)

    # testing load method
    loaded_data = reader.load(num_jets=-1)

    # check if -1 is passed, all data is loaded
    if not equal_jets:
        expected_shape = (num * (num + 1) / 2 * length,)
        assert loaded_data["jets"].shape == expected_shape

    # check not passing variables explicitly uses all variables
    assert len(loaded_data["jets"].dtype.names) == 2


@pytest.mark.parametrize("batch_size", [10_000, 11_001, 50_123, 101_234])
@pytest.mark.parametrize("num_jets", [100_000, 200_000])
def test_estimate_available_jets(batch_size, num_jets):
    fname, _ = get_mock_file(num_jets=num_jets)
    reader = H5Reader(fname, batch_size=batch_size, shuffle=False)
    with h5py.File(reader.files[0]) as f2:
        jets = f2["jets"][:]

    cuts = Cuts.from_list(["pt > 50"])
    estimated_num_jets = reader.estimate_available_jets(cuts, num=100_000)
    actual_num_jets = np.sum(jets["pt"] > 50)
    assert estimated_num_jets <= actual_num_jets
    assert estimated_num_jets > 0.95 * actual_num_jets

    cuts = Cuts.from_list(["HadronConeExclTruthLabelID == 5"])
    estimated_num_jets = reader.estimate_available_jets(cuts, num=100_000)
    actual_num_jets = np.sum(jets["HadronConeExclTruthLabelID"] == 5)
    assert estimated_num_jets <= actual_num_jets
    assert estimated_num_jets > 0.95 * actual_num_jets

    # check that the estimate_available_jets function returns the same
    # number of jets on subsequent calls
    assert reader.estimate_available_jets(cuts, num=100_000) == estimated_num_jets


@pytest.mark.parametrize("equal_jets", [True, False])
@pytest.mark.parametrize("cuts_list", [["x != -1"], ["x != 1"], ["x == -1"]])
def test_equal_jets_estimate(equal_jets, cuts_list):
    # fix the seed to make the test deterministic
    np.random.seed(42)

    # create test files (of different lengths)
    total_files = 2
    length = 200_000
    batch_size = 10_000
    tmpdirs = []
    actual_available_jets = []
    for i in range(1, total_files + 1):
        fname = NamedTemporaryFile(suffix=".h5", dir=mkdtemp()).name
        tmpdirs.append(Path(fname).parent)

        with h5py.File(fname, "w") as f:
            permutation = np.random.permutation(length * i)

            data = i * np.ones((length * i, 2))
            data[(length // 2) :, :] = i + 10
            data = data[permutation]
            x = data[:, 0]
            data = u2s(data, dtype=[("x", "f4"), ("y", "f4")])
            f.create_dataset("jets", data=data)

            data = i * np.ones((length * i, 40, 2))
            data[(length // 2) :, :, :] = i + 10
            data = data[permutation]
            data = u2s(data, dtype=[("a", "f4"), ("b", "f4")])
            f.create_dataset("tracks", data=data)

            # record how many jets would remain after cuts
            cut_condition = eval(cuts_list[0])
            actual_available_jets.append(x[cut_condition].shape[0])

    # calculate the actual number of available jets after cuts
    if equal_jets:
        actual_available_jets = min(actual_available_jets) * total_files
    else:
        actual_available_jets = sum(actual_available_jets)

    # create a multi-path sample
    sample = Sample([f"{x}/*.h5" for x in tmpdirs], name="test")

    # test reading from multiple paths
    reader = H5Reader(sample.path, batch_size=batch_size, equal_jets=equal_jets)

    # estimate available jets with given cuts
    cuts = Cuts.from_list(cuts_list)
    estimated_num_jets = reader.estimate_available_jets(cuts, num=100_000)

    # These values should be approximately correct, but with the given random seed they are exact
    assert actual_available_jets >= estimated_num_jets
    assert estimated_num_jets - actual_available_jets <= 1000


def test_reader_transform():
    fname, _ = get_mock_file()

    transform = Transform({
        "jets": {
            "pt": "pt_new",
            "silent": "silent",
        }
    })

    reader = H5Reader(fname, transform=transform, batch_size=1)
    data = reader.load(num_jets=10)

    assert "pt_new" in data["jets"].dtype.names


@pytest.fixture
def singlereader():
    fname, _ = get_mock_file()
    return H5SingleReader(fname, batch_size=10, do_remove_inf=True)


@pytest.fixture
def reader():
    fname, _ = get_mock_file()
    return H5Reader(fname, batch_size=10)


def test_remove_inf_no_inf_values(singlereader):
    data = {"jets": np.array([(1, 2.0), (3, 4.0)], dtype=[("pt", "f4"), ("eta", "f4")])}
    assert (singlereader.remove_inf(data)["jets"] == data["jets"]).all()


def test_remove_inf_with_inf_values(singlereader):
    _, f = get_mock_file()
    data = {"jets": f["jets"][:100], "tracks": f["tracks"][:100]}
    data["jets"]["pt"] = np.inf
    result = singlereader.remove_inf(data)
    assert len(result["jets"]) == 0
    assert len(result["tracks"]) == 0

    _, f = get_mock_file()
    data = {"jets": f["jets"][:100], "tracks": f["tracks"][:100]}
    data["jets"]["pt"] = 1
    data["jets"]["pt"][0] = np.inf
    result = singlereader.remove_inf(data)
    assert len(result["jets"]) == 99
    assert len(result["tracks"]) == 99

    _, f = get_mock_file()
    data = {"jets": f["jets"][:100], "tracks": f["tracks"][:100]}
    data["tracks"]["d0"] = 1
    data["tracks"]["d0"][0] = np.inf
    result = singlereader.remove_inf(data)
    assert len(result["jets"]) == 99
    assert len(result["tracks"]) == 99


def test_remove_inf_all_inf_values(singlereader):
    # Test with input data containing all infinity values, the result should have no data
    data = {
        "jets": np.array(
            [(1, np.inf), (3, np.inf), (5, np.inf)],
            dtype=[("pt", "f4"), ("eta", "f4")],
        ),
        "muons": np.array(
            [(0, np.inf), (3, np.inf), (5, np.inf)],
            dtype=[("pt", "f4"), ("eta", "f4")],
        ),
    }
    result = singlereader.remove_inf(data)
    assert {len(result[k]) for k in result} == {0}


def test_reader_shapes(reader):
    assert reader.shapes(10) == {"jets": (10,)}
    assert reader.shapes(10, ["jets"]) == {"jets": (10,)}


def test_reader_dtypes(reader):
    with h5py.File(reader.files[0]) as f:
        expected_dtype = {"jets": f["jets"].dtype, "tracks": f["tracks"].dtype}
    assert reader.dtypes() == expected_dtype
    print(reader.dtypes({"jets": ["pt"]}))


def test_get_attr(singlereader):
    assert singlereader.get_attr("test") == "test"


def test_stream(singlereader):
    print(singlereader.stream())
    total = 0
    for batch in singlereader.stream():
        total += len(batch["jets"])
    assert total == singlereader.num_jets


def test_weighting_two_files_100_vs_900(tmp_path):
    # Create two files: one with 100 jets, one with 900
    jets_100 = np.zeros(100, dtype=[("pt", "f4"), ("source", "i4")])
    jets_100["pt"] = 1.0
    jets_100["source"] = 0

    jets_900 = np.zeros(900, dtype=[("pt", "f4"), ("source", "i4")])
    jets_900["pt"] = 2.0
    jets_900["source"] = 1

    def write_file(path: Path, jets):
        with h5py.File(path, "w") as f:
            f.create_dataset("jets", data=jets)

    fpath_100 = tmp_path / "f100.h5"
    fpath_900 = tmp_path / "f900.h5"
    write_file(fpath_100, jets_100)
    write_file(fpath_900, jets_900)

    reader = H5Reader([fpath_100, fpath_900], batch_size=100, shuffle=False)

    # Check the weights are approximately 0.1 and 0.9
    expected_weights = [0.1, 0.9]
    np.testing.assert_allclose(reader.weights, expected_weights, rtol=1e-2)

    counts = {0: 0, 1: 0}
    total = 0
    for batch in reader.stream({"jets": ["pt", "source"]}):
        srcs = batch["jets"]["source"]
        total += len(srcs)
        # Correct split per batch
        assert np.sum(srcs == 0) == 10
        assert np.sum(srcs == 1) == 90
        for s in np.unique(srcs):
            counts[s] += np.sum(srcs == s)

    assert total == 1000
    assert counts[0] + counts[1] == 1000
    # Check the source 0 (file with 100 jets) contributes ~100
    assert 80 <= counts[0] <= 120
    assert 880 <= counts[1] <= 920


def test_skip_batches(tmp_path):
    # Create a file with 500 jets and identifiable indices
    num_jets = 500
    batch_size = 100

    jets = np.zeros(num_jets, dtype=[("pt", "f4"), ("index", "i4")])
    jets["pt"] = 42.0
    jets["index"] = np.arange(num_jets)

    fpath = tmp_path / "skip_test.h5"
    with h5py.File(fpath, "w") as f:
        f.create_dataset("jets", data=jets)

    reader = H5Reader(fpath, batch_size=batch_size, shuffle=False)

    # Skip first 2 batches (i.e., skip jets 0-199)
    skip_batches = 2
    indices_seen = []
    for batch in reader.stream({"jets": ["index"]}, skip_batches=skip_batches):
        indices_seen.extend(batch["jets"]["index"])

    # Check that skipped jets are not in the result
    assert all(i >= skip_batches * batch_size for i in indices_seen)
    assert len(indices_seen) == num_jets - skip_batches * batch_size
    assert indices_seen[0] == skip_batches * batch_size


@pytest.fixture
def h5_files(tmp_path):
    # Create 3 files with different number of jets
    # Total of 10k jets
    num_jets_list = [8000, 1500, 500]
    file_paths = [tmp_path / f"file_{i}.h5" for i in range(len(num_jets_list))]
    for i, (path, num_jets) in enumerate(zip(file_paths, num_jets_list)):
        with h5py.File(path, "w") as f:
            jets = np.zeros(num_jets, dtype=[("value", "i4"), ("index", "i4")])
            jets["value"] = i
            jets["index"] = np.arange(num_jets)
            f.create_dataset("jets", data=jets)
    return file_paths


def test_batch_reader_invalid_idx(h5_files):
    reader = H5Reader(h5_files, batch_size=1000, shuffle=False)

    batch_reader = reader.get_batch_reader(
        variables={"jets": ["value", "index"]},
    )
    with pytest.raises(AssertionError, match="Index must be non-negative"):
        batch_reader(-1)

    assert batch_reader(10) is None, "Batch reader should return None for out-of-bounds index"


def test_batch_reader(h5_files):
    reader = H5Reader(h5_files, batch_size=1000, shuffle=False)

    batch_reader = reader.get_batch_reader(
        variables=None,
    )

    index = list(range(10))
    np.random.shuffle(index)

    for i in index:
        batch = batch_reader(i)
        assert "jets" in batch
        assert len(batch["jets"]) == 1000
        assert "value" in batch["jets"].dtype.names
        assert "index" in batch["jets"].dtype.names

        for j, n in enumerate([8000, 1500, 500]):
            # First, we ensure that we have the correct number from each file in the batch
            sel = batch["jets"][batch["jets"]["value"] == j]
            assert sel["value"].shape[0] == int(n * 0.1)
            # Next, each file has series indices in the range [0, n)
            # so we check that the indices are within the expected range
            lower = int(i * 1000 / 10000 * n)
            upper = int((i + 1) * 1000 / 10000 * n)
            assert sel["index"].min() >= lower
            assert sel["index"].max() < upper


def test_single_batch_reader(h5_files):
    reader = H5SingleReader(h5_files[0], batch_size=1000, shuffle=False)

    batch_reader = reader.get_batch_reader()

    index = list(range(8))
    np.random.shuffle(index)

    for i in index:
        batch = batch_reader(i)
        assert "jets" in batch
        assert len(batch["jets"]) == 1000
        assert "value" in batch["jets"].dtype.names
        assert "index" in batch["jets"].dtype.names

        # First, we ensure that we have the correct number from each file in the batch
        sel = batch["jets"][batch["jets"]["value"] == 0]
        assert sel["value"].shape[0] == 1000
        # Next, each file has series indices in the range [0, n)
        # so we check that the indices are within the expected range
        lower = i * 1000
        upper = (i + 1) * 1000
        assert sel["index"].min() >= lower
        assert sel["index"].max() < upper
    assert batch_reader(10) is None, "Batch reader should return None for out-of-bounds index"
