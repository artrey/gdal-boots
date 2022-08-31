import numpy as np
from osgeo import gdal

from gdal_boots import Resampling
from gdal_boots.geometry import srs_from_epsg


def test_gdal_warp(ds_4326_factory):
    raster_ds = ds_4326_factory(shape=(10, 10))
    gdal_ds = raster_ds.ds

    ds = gdal.Warp(
        "",
        [gdal_ds],
        dstSRS=srs_from_epsg(3857),
        xRes=None,
        yRes=None,
        outputBounds=(0.2, 2.5, 2.0, 6.0),
        outputBoundsSRS=srs_from_epsg(4326),
        resampleAlg=Resampling.near.value,
        format="MEM",
    )
    data = ds.ReadAsArray()

    assert data.shape == (4, 2)
    assert np.array_equal(
        data,
        [
            [41, 42],
            [51, 52],
            [61, 62],
            [71, 72],
        ],
    )
