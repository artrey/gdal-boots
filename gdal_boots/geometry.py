# from __future__ import annotations

import logging
import typing as ty

from osgeo import gdal, ogr, osr
from osgeo.osr import SpatialReference

logger = logging.getLogger(__name__)

RawGeometry = ty.Union[str, dict, ogr.Geometry]


class SrsProxy:
    def __init__(self, srs: ty.Optional[SpatialReference] = None, epsg: ty.Optional[int] = None):
        if srs is None and epsg is None:
            raise ValueError("all parameters are None")
        if srs is None:
            srs = srs_from_epsg(epsg)
        self.srs = srs


class GeometryProxy:
    def __init__(self, geom: RawGeometry, srs_proxy: SrsProxy):
        if not isinstance(geom, ogr.Geometry):
            geom = GeometryBuilder().create(geom)
        self.geom = geom
        self.srs = srs_proxy.srs

    def transform(self, srs_proxy: SrsProxy) -> "GeometryProxy":
        return GeometryProxy(transform_by_srs(self.geom, self.srs, srs_proxy.srs), srs_proxy)


class GeometryBuilder:
    def __init__(self, flatten: bool = True):
        self.flatten = flatten

    def __call__(self, geometry: ty.Union[str, dict]) -> ogr.Geometry:
        return self.create(geometry)

    def create(self, geometry: ty.Union[str, dict]) -> ogr.Geometry:
        if isinstance(geometry, str):
            return ogr.CreateGeometryFromJson(geometry)

        geometry_type_lower = geometry["type"].lower()
        try:
            handler = getattr(self, f"create_{geometry_type_lower}")
        except AttributeError:
            raise ValueError(f"{geometry_type_lower} is not supported")

        return handler(**geometry)

    def create_polygon(self, coordinates: ty.Sequence, **kwargs) -> ogr.Geometry:
        polygon = ogr.Geometry(ogr.wkbPolygon)
        for ring_coords in coordinates:
            polygon.AddGeometry(self.create_linearring(ring_coords))
        return polygon

    def create_linearring(self, coordinates: ty.List[ty.Sequence], **kwargs) -> ogr.Geometry:
        return self._add_points(ogr.Geometry(ogr.wkbLinearRing), coordinates)

    def create_linestring(self, coordinates: ty.Sequence, **kwargs) -> ogr.Geometry:
        return self._add_points(ogr.Geometry(ogr.wkbLineString), coordinates)

    def create_multipolygon(self, coordinates: ty.Sequence, **kwargs) -> ogr.Geometry:
        multipolygon = ogr.Geometry(ogr.wkbMultiPolygon)
        for polygon_coordinates in coordinates:
            multipolygon.AddGeometry(self.create_polygon(polygon_coordinates))
        return multipolygon

    def create_point(self, coordinates: ty.Sequence, **kwargs) -> ogr.Geometry:
        return self._add_point(ogr.Geometry(ogr.wkbPoint), coordinates)

    def create_geometrycollection(self, geometries: ty.List[dict], **kwargs) -> ogr.Geometry:
        collection = ogr.Geometry(ogr.wkbGeometryCollection)
        for geometry in geometries:
            collection.AddGeometry(self.create(geometry))
        return collection

    def create_multilinestring(self, coordinates: ty.List[ty.Sequence], **kwargs) -> ogr.Geometry:
        linestring = ogr.Geometry(ogr.wkbMultiLineString)
        for line_coordinates in coordinates:
            linestring.AddGeometry(self.create_linestring(line_coordinates))
        return linestring

    def _add_point(self, geometry: ogr.Geometry, point: ty.Sequence) -> ogr.Geometry:
        if self.flatten:
            geometry.AddPoint_2D(*point[:2])
        else:
            geometry.AddPoint(*point)
        return geometry

    def _add_points(self, geometry: ogr.Geometry, points: ty.Sequence) -> ogr.Geometry:
        for point in points:
            self._add_point(geometry, point)
        return geometry


class GeometryGeoJson:
    def __init__(self, precision: int = None):
        self.precision = precision or 15

    def convert(self, geometry: ogr.Geometry) -> dict:
        geometry_type_lower = geometry.GetGeometryName().lower()
        try:
            handler = getattr(self, f"convert_{geometry_type_lower}")
        except AttributeError:
            raise ValueError(f"{geometry_type_lower} is not supported")
        return handler(geometry)

    def convert_polygon(self, geometry: ogr.Geometry) -> (str, dict):
        coordinates = [self._get_points(geometry.GetGeometryRef(i)) for i in range(geometry.GetGeometryCount())]
        return {"type": "Polygon", "coordinates": coordinates}

    def convert_multipolygon(self, geometry: ogr.Geometry) -> (str, dict):
        coordinates = []
        for i in range(geometry.GetGeometryCount()):
            sub_geom = geometry.GetGeometryRef(i)
            sub_coordinates = self.convert_polygon(sub_geom)["coordinates"]
            coordinates.append(sub_coordinates)
        return {"type": "MultiPolygon", "coordinates": coordinates}

    def convert_point(self, geometry: ogr.Geometry) -> (str, dict):
        return {"type": "Point", "coordinates": self._get_points(geometry)[0]}

    def convert_geometrycollection(self, geometry: ogr.Geometry) -> (str, dict):
        geometries = []
        for i in range(geometry.GetGeometryCount()):
            geometries.append(self.convert(geometry.GetGeometryRef(i)))
        return {"type": "GeometryCollection", "geometries": geometries}

    def convert_multilinestring(self, geometry: ogr.Geometry) -> (str, dict):
        lines = []
        for i in range(geometry.GetGeometryCount()):
            lines.append(self._get_points(geometry.GetGeometryRef(i)))
        return {"type": "MultiLineString", "coordinates": lines}

    def _get_points(self, geometry: ogr.Geometry) -> list:
        return [[round(c, self.precision) for c in p] for p in geometry.GetPoints()]


def to_geojson(geometry: ogr.Geometry, flatten: bool = True, precision: int = None) -> dict:
    if flatten:
        geometry.FlattenTo2D()

    return GeometryGeoJson(precision=precision).convert(geometry)


def srs_from_epsg(epsg: int) -> SpatialReference:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    return srs


def transform_by_srs(geometry: ogr.Geometry, from_srs: SpatialReference, to_srs: SpatialReference) -> ogr.Geometry:
    from_srs = from_srs.Clone()
    from_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    to_srs = to_srs.Clone()
    to_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transformation = osr.CoordinateTransformation(from_srs, to_srs)
    result_geometry = geometry.Clone()
    result_geometry.Transform(transformation)
    return result_geometry


def transform(geometry: ogr.Geometry, from_epsg: int, to_epsg: int) -> ogr.Geometry:
    from_srs = srs_from_epsg(from_epsg)
    to_srs = srs_from_epsg(to_epsg)
    return transform_by_srs(geometry, from_srs=from_srs, to_srs=to_srs)


def transform_geojson(
    geometry: dict,
    from_epsg: int,
    to_epsg: int,
    flatten: bool = True,
    precision: int = None,
) -> dict:
    ogr_geometry = GeometryBuilder(flatten=flatten).create(geometry)
    new_geometry = transform(ogr_geometry, from_epsg, to_epsg)
    return to_geojson(new_geometry, flatten=False, precision=precision)


def make_valid_geojson(geometry: dict, precision: int = None) -> dict:
    gdal_geometry: ogr.Geometry = GeometryBuilder().create(geometry)
    gdal_geometry = make_valid(gdal_geometry)
    return GeometryGeoJson(precision=precision).convert(gdal_geometry)


def make_valid(geometry: ogr.Geometry) -> ogr.Geometry:
    geometry.CloseRings()
    valid_geometry = geometry.MakeValid()
    if valid_geometry is None:
        raise RuntimeError(gdal.GetLastErrorMsg())

    if valid_geometry.GetGeometryName() == "GEOMETRYCOLLECTION":
        geometry_type = geometry.GetGeometryName()
        union_geometry = ogr.Geometry(ogr.wkbMultiPolygon if geometry_type == "MULTIPOLYGON" else ogr.wkbPolygon)
        for i in range(valid_geometry.GetGeometryCount()):
            sub_geometry = valid_geometry.GetGeometryRef(i)

            if sub_geometry.GetGeometryName() not in ["MULTIPOLYGON", "POLYGON"]:
                continue
            union_geometry = union_geometry.Union(sub_geometry)

        valid_geometry.Destroy()
        valid_geometry = union_geometry

        # cast to input type if possible
        if geometry_type == "MULTIPOLYGON" and valid_geometry.GetGeometryName() == "POLYGON":
            _geometry = ogr.Geometry(ogr.wkbMultiPolygon)
            _geometry.AddGeometry(valid_geometry)
            valid_geometry = _geometry

        return valid_geometry

    return valid_geometry


def calc_best_resolution_bbox(
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    orig_resolution: ty.Tuple[float, float],
) -> ty.Tuple[float, float]:
    dx = x_max - x_min
    dy = y_max - y_min
    cells_x = max(1, round(dx / orig_resolution[0] + 10e-9))  # 10e-9 for "true" rounding: 8.5 -> 9
    cells_y = max(1, round(dy / orig_resolution[1] + 10e-9))
    return dx / cells_x, dy / cells_y


def calc_best_resolution(geometry: RawGeometry, orig_resolution: ty.Tuple[float, float]) -> ty.Tuple[float, float]:
    if not isinstance(geometry, ogr.Geometry):
        geometry = GeometryBuilder().create(geometry)
    x_min, x_max, y_min, y_max = geometry.GetEnvelope()
    return calc_best_resolution_bbox(x_min, x_max, y_min, y_max, orig_resolution)
