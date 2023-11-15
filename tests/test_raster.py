import io
import json
import os.path
import sys
import tempfile

import affine
import numpy as np
import pytest
import shapely.geometry
import tqdm
from osgeo import gdal
from threadpoolctl import threadpool_limits

from gdal_boots import gdal_version
from gdal_boots.gdal import GeoInfo, RasterDataset, Resampling, VectorDataset
from gdal_boots.geometry import GeometryBuilder, to_geojson
from gdal_boots.geometry import transform as geometry_transform
from gdal_boots.options import GPKG, PNG, GTiff, JP2OpenJPEG

np.random.seed(31415926)


def test_open_file(lena_512_png):
    with RasterDataset.open(lena_512_png) as ds:
        assert ds
        assert ds.shape == (3, 512, 512)
        assert ds[:, :100, :100].shape == (3, 100, 100)

        png_data = ds.to_bytes(PNG(zlevel=9))

        with tempfile.NamedTemporaryFile(suffix=".png") as fd:
            with open(fd.name, "wb") as fd:
                fd.write(png_data)

        assert len(png_data) == 476208


@pytest.mark.skipif(
    not all(
        [
            os.path.exists(filename)
            for filename in [
                "tests/fixtures/extra/S2A_MSIL1C_T38TLR_20170518_B08_bad.jp2",
                "tests/fixtures/extra/B04.tif",
            ]
        ]
    ),
    reason="extra files do not exist",
)
def test_is_valid():
    with RasterDataset.open("tests/fixtures/extra/S2A_MSIL1C_T38TLR_20170518_B08_bad.jp2") as ds:
        assert not ds.is_valid()

    with RasterDataset.open("tests/fixtures/extra/B04.tif") as ds:
        assert ds.is_valid()


def test_open_memory(lena_512_png):
    with open(lena_512_png, "rb") as fd:
        data = fd.read()

    with RasterDataset.from_bytes(data) as ds:
        assert ds.shape == (3, 512, 512)
        assert ds[:, :100, :100].shape == (3, 100, 100)

        png_data = ds.to_bytes(PNG(zlevel=9))
        assert len(png_data) == 476208

        tiff_data = ds.to_bytes(GTiff(zlevel=9))

    with RasterDataset.from_bytes(tiff_data, open_flag=gdal.OF_RASTER | gdal.GA_Update) as ds:
        assert ds.shape

    stream = io.BytesIO(tiff_data)
    with RasterDataset.from_stream(stream, open_flag=gdal.OF_RASTER | gdal.GA_Update) as ds:
        assert ds.shape


def test_create():
    img = np.random.randint(0, 255, size=(1098, 1098), dtype=np.uint8)
    img[100:200, 100:200] = 192
    img[800:900, 800:900] = 250

    geoinfo = GeoInfo(epsg=32631, transform=affine.Affine(10.0, 0.0, 600000.0, 0.0, -10.0, 5700000.0))

    with RasterDataset.create(shape=img.shape, dtype=img.dtype.type, geoinfo=geoinfo) as ds:
        ds[:, :] = img

        with tempfile.NamedTemporaryFile(suffix=".png") as fd:
            ds.to_file(fd.name, PNG())
            data = fd.read()
            assert len(data) == 1190120
            assert data[:4] == b"\x89PNG"

        with tempfile.NamedTemporaryFile(suffix=".tiff") as fd:
            ds.to_file(fd.name, GTiff())
            data = fd.read()
            assert len(data) == 1206004
            assert data[:3] == b"II*"

            assert len(ds.to_bytes(GTiff())) == len(data)

        if gdal_version < (3, 6, 3):
            pytest.skip("known bug connected with all_touched=True")

        with tempfile.NamedTemporaryFile(suffix=".jp2") as fd:
            ds.to_file(fd.name, JP2OpenJPEG())
            data = fd.read()
            assert len(data) == 303317
            assert data[:6] == b"\x00\x00\x00\x0cjP"

            assert len(ds.to_bytes(JP2OpenJPEG())) == len(data)


def test_vectorize():
    img = np.full((1098, 1098), 64, dtype=np.uint8)
    img[10:200, 10:200] = 192
    img[800:900, 800:900] = 250

    geoinfo = GeoInfo(epsg=32631, transform=affine.Affine(10.0, 0.0, 600000.0, 0.0, -10.0, 5700000.0))

    from typing import Any, Callable

    import tqdm

    with tqdm.tqdm(total=100) as pbar:
        tqdm_progress: Callable[[float, str, Any], None] = lambda n, msg, _: pbar.update(int(round(n * 100 - pbar.n)))
        with RasterDataset.create(shape=img.shape, dtype=img.dtype.type, geoinfo=geoinfo) as ds:
            ds[:, :] = img

            # v_ds = ds.to_vector(callback=gdal.TermProgress)
            v_ds = ds.to_vector(callback=tqdm_progress)
            assert v_ds
            with tempfile.NamedTemporaryFile(suffix=".gpkg") as fd:
                v_ds.to_file(fd.name, GPKG())

            with tempfile.NamedTemporaryFile(suffix=".gpkg") as fd:

                with pytest.raises(RuntimeError):
                    v_ds.to_file(fd.name, GPKG(), overwrite=False)


def test_memory():
    import json

    from osgeo import gdal, ogr

    gdal.UseExceptions()

    geojson = json.dumps({"type": "Point", "coordinates": [27.773437499999996, 53.74871079689897]})
    srcdb = gdal.OpenEx(geojson, gdal.OF_VECTOR | gdal.OF_VERBOSE_ERROR)
    # # srcdb = ogr.Open(geojson)
    # print('type', srcdb, type(srcdb))
    # gdal.VectorTranslate('test.gpkg', srcdb, format='GPKG')
    # return

    # create an output datasource in memory
    outdriver = ogr.GetDriverByName("MEMORY")
    source = outdriver.CreateDataSource("memData")

    # open the memory datasource with write access
    outdriver.Open("memData", 1)

    # copy a layer to memory
    source.CopyLayer(srcdb.GetLayer(), "pipes", ["OVERWRITE=YES"])

    # the new layer can be directly accessed via the handle pipes_mem or as source.GetLayer('pipes'):
    layer = source.GetLayer("pipes")
    layer.CreateField(ogr.FieldDefn("field", ogr.OFTReal))

    for feature in layer:
        feature.SetField("field", 1.0)

    with tempfile.TemporaryDirectory() as tmp_dir:
        gdal.VectorTranslate(f"{tmp_dir}/test.gpkg", srcdb, format="GPKG")


def test_warp_extra():
    ds1 = RasterDataset.create((100, 100), dtype=np.uint8)
    ds1.set_bounds([(0, 0), (10_000, 10_000)], epsg=3857)
    ds1[:] = 1
    ds2 = RasterDataset.create((100, 100), dtype=np.uint8)
    ds2.set_bounds([(10_000, 0), (20_000, 10_000)], epsg=3857)
    ds2[:] = 2

    ds_merged = ds1.warp(extra_ds=[ds2])

    assert ds_merged.shape == (100, 200)


def test_warp_extra_multiband_simple():
    ds1 = RasterDataset.create((2, 100, 100), dtype=np.uint8)
    ds1.set_bounds([(0, 0), (10_000, 10_000)], epsg=3857)
    ds1[0, :, :] = 1
    ds1[1, :, :] = 2

    ds2 = RasterDataset.create((2, 100, 100), dtype=np.uint8)
    ds2.set_bounds([(10_000, 0), (20_000, 10_000)], epsg=3857)
    ds2[0, :, :] = 3
    ds2[1, :, :] = 4

    ds_merged = ds1.warp(extra_ds=[ds2])

    assert ds_merged.shape == (2, 100, 200)
    assert np.all(np.unique(ds_merged[0, :]) == [1, 3])
    assert np.all(np.unique(ds_merged[1, :]) == [2, 4])


def test_warp_extra_multiband_3857():
    ds1 = RasterDataset.create((2, 517, 516), dtype=np.uint8)
    ds1.set_bounds([[2584541.63003097, 6381461.18550703], [2616451.28106481, 6413432.67694985]], epsg=3857)
    ds1[:] = 255
    ds1[0, :, :] = 1
    ds1[1, :, :] = 2
    ds1.nodata = [255, 255]

    ds2 = RasterDataset.create((2, 517, 516), dtype=np.uint8)
    ds2.set_bounds([[2585456.81116125, 6412469.47957801], [2617484.73133488, 6444559.46936438]], epsg=3857)
    ds2[:] = 255
    ds2[0, :, :] = 3
    ds2[1, :, :] = 4
    ds2.nodata = [255, 255]

    ds_merged = ds1.warp(extra_ds=[ds2])

    assert np.all(np.unique(ds_merged[0, :]) == [1, 3, 255]), np.unique(ds_merged[0, :])
    assert np.all(np.unique(ds_merged[1, :]) == [2, 4, 255]), np.unique(ds_merged[0, :])


def test_warp_cutline():
    ds = RasterDataset.create((1, 400, 400), dtype=np.uint8)
    ds.set_bounds([(2320000, 6820000), (2360000, 6860000)], epsg=3857)
    ds[:] = np.array([32, 64, 128, 255]).reshape(2, 2).repeat(200, axis=0).repeat(200, axis=1)
    ds.nodata = 0

    geojson = {
        "crs": {"properties": {"name": "urn:ogc:def:crs:EPSG::3857"}, "type": "name"},
        "features": [
            {
                "geometry": {
                    "coordinates": [
                        [
                            [2332115.0, 6854380.0],
                            [2323410.0, 6838275.0],
                            [2333276.0, 6826088.0],
                            [2336178.0, 6842628.0],
                            [2354748.0, 6831021.0],
                            [2352572.0, 6850607.0],
                            [2332115.0, 6854380.0],
                        ]
                    ],
                    "type": "Polygon",
                },
                "id": 0,
                "properties": {"name": "test"},
                "type": "Feature",
            }
        ],
        "type": "FeatureCollection",
    }

    vds = VectorDataset.open(json.dumps(geojson))

    ds_warped = ds.warp(resampling=Resampling.near, cutline=vds)
    assert ds_warped.shape == (282, 312)

    unique = np.unique(ds_warped[:], return_counts=True)

    assert np.all(unique[0] == [0, 32, 64, 128, 255])
    assert np.all(unique[1] == [41494, 15742, 15412, 9333, 6003])

    with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w+", delete=False) as fd:
        json.dump(geojson, fd)

    ds_warped = ds.warp(resampling=Resampling.near, cutline=fd.name)
    os.unlink(fd.name)

    unique = np.unique(ds_warped[:], return_counts=True)

    assert np.all(unique[0] == [0, 32, 64, 128, 255])
    assert np.all(unique[1] == [41494, 15742, 15412, 9333, 6003])


@pytest.mark.skipif(
    not os.path.exists("tests/fixtures/extra/B04.tif"),
    reason='extra file "tests/fixtures/extra/B04.tif" does not exist',
)
def test_warp(minsk_polygon):
    bbox = shapely.geometry.shape(minsk_polygon).bounds

    with RasterDataset.open("tests/fixtures/extra/B04.tif") as ds:
        warped_ds = ds.warp(bbox, resolution=(10, 10))

        assert (warped_ds.geoinfo.transform.a, -warped_ds.geoinfo.transform.e) == (10, 10)

        with tempfile.NamedTemporaryFile(suffix=".tiff") as fd:
            warped_ds.to_file(fd.name, GTiff())

        warped_ds_r100 = ds.warp(bbox, resolution=(100, 100))

        assert (warped_ds_r100.geoinfo.transform.a, -warped_ds_r100.geoinfo.transform.e) == (100, 100)
        assert all((np.array(warped_ds.shape) / 10).round() == warped_ds_r100.shape)


@pytest.mark.skipif(
    not os.path.exists("tests/fixtures/extra/"),
    reason='extra folder "tests/fixtures/extra/" does not exist',
)
def test_fast_warp():
    with open("tests/fixtures/35UNV_field_small.geojson") as fd:
        test_field = json.load(fd)
        geometry_4326 = GeometryBuilder().create(test_field)

    def _get_bbox(epsg):
        utm_geometry = geometry_transform(geometry_4326, 4326, epsg)
        bbox = utm_geometry.GetEnvelope()
        return np.array(bbox).reshape(2, 2).T.reshape(-1)

    with RasterDataset.open("tests/fixtures/extra/B02_10m.jp2") as ds:
        bbox = _get_bbox(ds.geoinfo.epsg)

        with tempfile.NamedTemporaryFile(prefix="10m_", suffix=".tiff") as fd:
            ds_warp = ds.fast_warp(bbox)
            ds_warp.to_file(fd.name, GTiff())

            assert ds_warp.shape == (8, 9)
            assert np.all(ds_warp.bounds() == np.array([[509040.0, 5946040.0], [509130.0, 5946120.0]]))
            assert ds_warp.dtype == ds.dtype

            img_warp, geoinfo = ds.fast_warp_as_array(bbox)

            assert np.all(img_warp == ds_warp[:])

    with RasterDataset.open("tests/fixtures/extra/B05_20m.jp2") as ds:
        bbox = _get_bbox(ds.geoinfo.epsg)

        with tempfile.NamedTemporaryFile(prefix="20m_", suffix=".tiff") as fd:
            ds_warp = ds.fast_warp(bbox)
            ds_warp.to_file(fd.name, GTiff())

            assert ds_warp
            assert np.all(ds_warp.bounds() == np.array([[509040.0, 5946040.0], [509140.0, 5946120.0]]))

    with RasterDataset.open("tests/fixtures/extra/B09_60m.jp2") as ds:
        bbox = _get_bbox(ds.geoinfo.epsg)

        with tempfile.NamedTemporaryFile(prefix="60m_", suffix=".tiff") as fd:
            ds_warp = ds.fast_warp(bbox)
            ds_warp.to_file(fd.name, GTiff())

            assert ds_warp.shape == (2, 2)
            assert np.all(ds_warp.bounds() == np.array([[509040.0, 5946000.0], [509160.0, 5946120.0]]))

        ds_10m = ds.warp(
            ds.bounds().reshape(-1),
            ds.geoinfo.epsg,
            resolution=(10, 10),
        )

        with tempfile.NamedTemporaryFile(prefix="60m_", suffix=".tiff") as fd:
            ds_warp = ds_10m.fast_warp(bbox)
            ds_warp.to_file(fd.name, GTiff())

            assert ds_warp.shape == (8, 9)
            assert np.all(ds_warp.bounds() == np.array([[509040.0, 5946040.0], [509130.0, 5946120.0]]))


@pytest.mark.skipif(
    not os.path.exists("tests/fixtures/extra/B04.tif"),
    reason='extra file "tests/fixtures/extra/B04.tif" does not exist',
)
def test_bounds():
    with RasterDataset.open("tests/fixtures/extra/B04.tif") as ds:
        assert np.all(
            ds.bounds()
            == [
                (499980.0, 5890200.0),
                (609780.0, 6000000.0),
            ]
        )
        assert np.all(
            ds.bounds(4326) == [(26.999700868340735, 53.16117354432605), (28.68033586831364, 54.136377428252246)]
        )

    with RasterDataset.create(shape=(100, 100), dtype=np.uint8) as ds:
        ds[:] = 255
        ds[1:99, 1:99] = 0
        ds.set_bounds(
            [
                (499980.0, 5890200.0),
                (609780.0, 6000000.0),
            ],
            32635,
        )
        assert np.all(
            ds.bounds(32635)
            == [
                (499980.0, 5890200.0),
                (609780.0, 6000000.0),
            ]
        )
        ds.set_bounds([(26.999700868340735, 53.16117354432605), (28.68033586831364, 54.136377428252246)], 4326)
        assert np.all(ds.bounds() == [(26.999700868340735, 53.16117354432605), (28.68033586831364, 54.136377428252246)])
        assert np.all(
            ds.bounds(32635).round()
            == [
                [499980.0, 5890200.0],
                [609780.0, 6000000.0],
            ]
        )
        result = to_geojson(ds.bounds_polygon(), precision=9)
        assert result == {
            "type": "Polygon",
            "coordinates": [
                [
                    [26.999700868, 53.161173544],
                    [28.680335868, 53.161173544],
                    [28.680335868, 54.136377428],
                    [26.999700868, 54.136377428],
                    [26.999700868, 53.161173544],
                ]
            ],
        }


def test_crop_by_geometry():
    ds1 = RasterDataset.create(
        shape=(1134, 1134),
        dtype=np.uint8,
        geoinfo=GeoInfo(
            epsg=32720,
            transform=affine.Affine(
                10.000000005946216, 0.0, 554680.0000046358, 0.0, -10.000000003180787, 6234399.99998708
            ),
        ),
    )
    ds1[:] = np.random.randint(64, 128, (1134, 1134), np.uint8)

    ds2 = RasterDataset.create(
        shape=(1134, 1134),
        dtype=np.uint8,
        geoinfo=GeoInfo(
            epsg=32720,
            transform=affine.Affine(
                10.000000005946317, 0.0, 554680.0000046354, 0.0, -10.00000000318243, 6245339.999990689
            ),
        ),
    )
    ds2[:] = np.random.randint(128, 192, (1134, 1134), np.uint8)

    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [-62.403073310852044, -34.02648590051866],
                [-62.40650653839111, -34.03818674708322],
                [-62.398738861083984, -34.03943142302355],
                [-62.395563125610344, -34.02780188173055],
                [-62.403073310852044, -34.02648590051866],
            ]
        ],
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        cropped_ds, mask = ds1.crop_by_geometry(geometry, extra_ds=[ds2])
        cropped_ds.to_file(f"{tmp_dir}/cropped.png", PNG())
        cropped_ds_r100, _ = ds1.crop_by_geometry(geometry, extra_ds=[ds2], resolution=(100, 100))
        cropped_ds_r100.to_file(f"{tmp_dir}/cropped_100.png", PNG())
        assert all((np.array(cropped_ds.shape) / 10).round() == cropped_ds_r100.shape)

    # crop by 3857
    with tempfile.TemporaryDirectory() as tmp_dir:
        geometry_4326 = GeometryBuilder().create(geometry)
        geometry_3857 = geometry_transform(geometry_4326, 4326, 3857)
        geometry_3857.FlattenTo2D()
        cropped_ds, mask = ds1.crop_by_geometry(geometry_3857, epsg=3857)
        cropped_ds.to_file(f"{tmp_dir}/cropped_by3857.tiff", GTiff())

    # crop to 3857
    with tempfile.TemporaryDirectory() as tmp_dir:
        cropped_ds_3857, mask = ds1.crop_by_geometry(geometry, out_epsg=3857)
        assert cropped_ds_3857.geoinfo.epsg == 3857
        cropped_ds_3857.to_file(f"{tmp_dir}/cropped_to3857.tiff", GTiff())

    small_geometry = shapely.geometry.mapping(shapely.geometry.shape(geometry).buffer(-0.003868))
    with pytest.raises(RuntimeError):
        ds1.crop_by_geometry(small_geometry)

    # crop by custom crs
    # https://epsg.io/102033
    aea_proj = (
        "+proj=aea +lat_0=-32 +lon_0=-60 +lat_1=-5 +lat_2=-42 +x_0=0 +y_0=0 +ellps=aust_SA +units=m +no_defs +type=crs"
    )
    cropped_ds, mask_ds = ds1.crop_by_geometry(geometry, extra_ds=[ds2], out_proj4=aea_proj, apply_mask=False)
    assert cropped_ds.geoinfo.proj4
    img = cropped_ds[:]
    assert (img.min(), img.max()) == (64, 191)

    img = mask_ds[:]
    assert (img.min(), img.max()) == (0, 1)


def test_write():
    img = np.ones((3, 5, 5))
    img[0] = 1
    img[1] = 2
    img[2] = 3

    ds = RasterDataset.create(shape=(3, 5, 5))
    ds[:] = 1
    ds[:] = img
    ds[0] = img[0]
    ds[:, 0] = 1
    # not supported
    # ds[:,0,:] = img[:,0]
    ds[1:3, 1:3, :] = 1
    ds[(0, 2), 2:5, 2:5] = img[(0, 2), :3, :3]

    ds = RasterDataset.create(shape=(10, 10))
    ds[2:5, 2:5] = 1


@pytest.mark.skipif(not os.getenv("TEST_COMPARE_WARP", ""), reason="skip comparison warp")
@pytest.mark.skipif(
    not os.path.exists("tests/fixtures/extra/B02_10m.jp2"),
    reason='extra file "tests/fixtures/extra/B02_10m.jp2" does not exist',
)
def test_compare_warp_fast_warp():
    np.random.randint(1622825326.8494937)

    with RasterDataset.open("tests/fixtures/extra/B02_10m.jp2") as ds:
        ds_bounds = ds.bounds()

        size = 1000
        hw_range = np.array([50, 500]) * ds.resolution

        xy = np.array(
            [
                np.random.randint(ds_bounds[0][0], ds_bounds[1][0] - hw_range[1], size),
                np.random.randint(ds_bounds[0][1], ds_bounds[1][1] - hw_range[1], size),
            ]
        )
        hw = np.array(
            [
                np.random.randint(*hw_range, size),
                np.random.randint(*hw_range, size),
            ]
        )

        bboxes = np.array([xy, xy + hw]).reshape(4, -1).T

        with threadpool_limits(limits=1, user_api="blas"):
            for bbox in tqdm.tqdm(bboxes):
                ds.fast_warp(bbox)

            for bbox in tqdm.tqdm(bboxes):
                ds.fast_warp_as_array(bbox)

        for bbox in tqdm.tqdm(bboxes):
            ds.warp(bbox, bbox_epsg=ds.geoinfo.epsg)


def test_meta_save_load():
    shape = (10, 10)
    ds = RasterDataset.create(
        shape=shape,
        dtype=np.uint8,
        geoinfo=GeoInfo(
            epsg=32720,
            transform=affine.Affine(
                10.000000005946216, 0.0, 554680.0000046358, 0.0, -10.000000003180787, 6234399.99998708
            ),
        ),
    )
    ds[:] = np.random.randint(64, 128, shape, np.uint8)

    def assert_metadata(expected_meta, meta):
        for k, v in expected_meta.items():
            assert k in meta
            assert meta[k] == v

    def check_meta(desired_meta: dict):
        formats = {"jp2": JP2OpenJPEG(), "tiff": GTiff(compress=GTiff.Compress.deflate)}

        for ext, driver in formats.items():
            # ds -> file -> ds
            with tempfile.NamedTemporaryFile(suffix=f".{ext}") as fd:
                ds.to_file(fd.name, driver)
                with RasterDataset.open(fd.name) as ds_loaded:
                    assert_metadata(desired_meta, ds_loaded.meta)

            # ds -> bytes -> file -> ds
            with tempfile.NamedTemporaryFile(suffix=f".{ext}") as fd:
                fd.write(ds.to_bytes(driver))
                fd.file.flush()
                with RasterDataset.open(fd.name) as ds_loaded:
                    assert_metadata(desired_meta, ds_loaded.meta)

            # ds -> bytes -> ds
            data_b = ds.to_bytes(driver)
            with RasterDataset.from_bytes(data_b) as ds_loaded:
                assert_metadata(desired_meta, ds_loaded.meta)

    meta = {"one": 1}
    ds.meta = meta
    check_meta(meta)

    meta["two"] = 2
    ds.meta = meta
    check_meta(meta)

    with pytest.raises(TypeError):
        ds.meta["not work"] = "not work"

    # python 3.9 feature
    if sys.version_info >= (3, 9, 0):
        meta |= {"test1": "string", "test2": 1.4}
        ds.meta |= {"test1": "string", "test2": 1.4}
    else:
        meta.update({"test1": "string", "test2": 1.4})
        ds_meta = dict(ds.meta)
        ds_meta.update({"test1": "string", "test2": 1.4})
        ds.meta = ds_meta

    check_meta(meta)


def test_raster_union():
    gi1 = GeoInfo(epsg=32628, transform=affine.Affine(10, 0, 0, 0, -10, 0))
    ds1 = RasterDataset.create(shape=(3, 3), geoinfo=gi1, dtype=int)
    ds1[:] = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]).T

    gi2 = GeoInfo(epsg=32628, transform=affine.Affine(10, 0, 10, 0, -10, 0))
    ds2 = RasterDataset.create(shape=(3, 3), geoinfo=gi2, dtype=int)
    ds2[:] = np.array([[4, 5, 6], [7, 8, 9], [1, 2, 3]]).T

    ds = ds1.union([ds2])
    assert np.array_equal(ds[:], np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9], [1, 2, 3]]).T)

    gi3 = GeoInfo(epsg=32628, transform=affine.Affine(10, 0, 0, 0, -10, 10))
    ds3 = RasterDataset.create(shape=(3, 3), geoinfo=gi3, dtype=int)
    ds3[:] = np.array([[3, 1, 2], [6, 4, 5], [9, 7, 8]]).T

    ds = ds1.union([ds2, ds3])
    assert np.array_equal(ds[:], np.array([[3, 1, 2, 3], [6, 4, 5, 6], [9, 7, 8, 9], [0, 1, 2, 3]]).T)


@pytest.mark.parametrize(
    "points,expected",
    [
        [[], []],
        [[{"type": "Point", "coordinates": [0, 0]}], [None]],
        [[{"type": "Point", "coordinates": [-1, -1]}], [None]],
        [[{"type": "Point", "coordinates": [0, 0.1]}], [11]],
        [[{"type": "Point", "coordinates": [0.2, 2.5]}], [1]],
        [[{"type": "Point", "coordinates": [2.9, 4.9]}], [None]],
        [[{"type": "Point", "coordinates": [3, 4.9]}], [None]],
        [[{"type": "Point", "coordinates": [2.9, 5]}], [None]],
        [[{"type": "Point", "coordinates": [3, 5]}], [None]],
        [[{"type": "Point", "coordinates": coord} for coord in [[0.2, 2.5], [0, 0.1], [10, 10]]], [1, 11, None]],
    ],
)
def test_values_by_points(points, expected):
    ds = RasterDataset.create(shape=(3, 5), dtype=int)
    ds[:] = np.array(range(1, ds.size + 1)).reshape(ds.shape)
    ds.set_bounds([(0, 0), ds.shape[::-1]], epsg=4326)

    assert ds.values_by_points(points) == expected


def test_values_by_points_multiband():
    ds = RasterDataset.create(shape=(2, 3, 5), dtype=int)
    ds[:] = np.array(range(1, ds.size + 1)).reshape(ds.shape)
    ds.set_bounds([(0, 0), ds.shape[-2:][::-1]], epsg=4326)

    value = ds.values_by_points([{"type": "Point", "coordinates": [0.2, 2.5]}])[0]
    assert np.array_equal(value, np.array([1, 16]))
