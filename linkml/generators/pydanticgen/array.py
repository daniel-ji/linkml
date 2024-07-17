import sys
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Iterable, Optional, TypeVar, Union, get_args

from linkml_runtime.linkml_model.meta import ArrayExpression, DimensionExpression
from pydantic import VERSION as PYDANTIC_VERSION

from linkml.generators.common.range import ArrayRangeGenerator as ArrayRangeGenerator_
from linkml.utils.deprecation import deprecation_warning

if int(PYDANTIC_VERSION[0]) >= 2:
    from pydantic_core import core_schema
else:
    # Support for having pydantic 1 installed in the same environment will be dropped in 1.9.0
    deprecation_warning("pydantic-v1")

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema

if sys.version_info.minor <= 8:
    from typing_extensions import Annotated
else:
    from typing import Annotated

from linkml.generators.pydanticgen.build import RangeResult
from linkml.generators.pydanticgen.template import ConditionalImport, Import, Imports, ObjectImport

class ArrayRepresentation(Enum):
    LIST = "list"
    NPARRAY = "nparray"  # numpy and nptyping must be installed to use this

class ArrayRangeGenerator(ArrayRangeGenerator_):
    representations = ArrayRepresentation


_T = TypeVar("_T")
_RecursiveListType = Iterable[Union[_T, Iterable["_RecursiveListType"]]]


class AnyShapeArrayType(Generic[_T]):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: "GetCoreSchemaHandler") -> "CoreSchema":
        # double-nested parameterized types here
        # source_type: List[Union[T,List[...]]]
        item_type = Any if get_args(get_args(source_type)[0])[0] is _T else get_args(get_args(source_type)[0])[0]

        item_schema = handler.generate_schema(item_type)
        if item_schema.get("type", "any") != "any":
            item_schema["strict"] = True

        if item_type is Any:
            # Before python 3.11, `Any` type was a special object without a __name__
            item_name = "Any"
        else:
            item_name = item_type.__name__

        array_ref = f"any-shape-array-{item_name}"

        schema = core_schema.definitions_schema(
            core_schema.list_schema(core_schema.definition_reference_schema(array_ref)),
            [
                core_schema.union_schema(
                    [
                        core_schema.list_schema(core_schema.definition_reference_schema(array_ref)),
                        item_schema,
                    ],
                    ref=array_ref,
                )
            ],
        )

        return schema


AnyShapeArray = Annotated[_RecursiveListType, AnyShapeArrayType]

_AnyShapeArrayImports = (
    Imports()
    + Import(
        module="typing",
        objects=[
            ObjectImport(name="Generic"),
            ObjectImport(name="Iterable"),
            ObjectImport(name="TypeVar"),
            ObjectImport(name="Union"),
            ObjectImport(name="get_args"),
        ],
    )
    + ConditionalImport(
        condition="sys.version_info.minor > 8",
        module="typing",
        objects=[ObjectImport(name="Annotated")],
        alternative=Import(module="typing_extensions", objects=[ObjectImport(name="Annotated")]),
    )
    + Import(module="pydantic", objects=[ObjectImport(name="GetCoreSchemaHandler")])
    + Import(module="pydantic_core", objects=[ObjectImport(name="CoreSchema"), ObjectImport(name="core_schema")])
)

# annotated types are special and inspect.getsource() can't stringify them
_AnyShapeArrayInjects = [
    '_T = TypeVar("_T")',
    '_RecursiveListType = Iterable[Union[_T, Iterable["_RecursiveListType"]]]',
    AnyShapeArrayType,
    "AnyShapeArray = Annotated[_RecursiveListType, AnyShapeArrayType]",
]

_ConListImports = Imports() + Import(module="pydantic", objects=[ObjectImport(name="conlist")])


class ListOfListsArray(ArrayRangeGenerator):
    """
    Represent arrays as lists of lists!

    TODO: Move all validation of values (eg. anywhere we raise a ValueError) to the ArrayExpression
    dataclass and out of the generator class
    """

    REPR = ArrayRepresentation.LIST

    @staticmethod
    def _list_of_lists(dimensions: int, dtype: str) -> str:
        return ("List[" * dimensions) + dtype + ("]" * dimensions)

    @staticmethod
    def _parameterized_dimension(dimension: DimensionExpression, dtype: str) -> RangeResult:
        # TODO: Preserve label representation in some readable way! doing the MVP now of using conlist
        if dimension.exact_cardinality and (dimension.minimum_cardinality or dimension.maximum_cardinality):
            raise ValueError("Can only specify EITHER exact_cardinality OR minimum/maximum cardinality")
        elif dimension.exact_cardinality:
            dmin = dimension.exact_cardinality
            dmax = dimension.exact_cardinality
        elif dimension.minimum_cardinality or dimension.maximum_cardinality:
            dmin = dimension.minimum_cardinality
            dmax = dimension.maximum_cardinality
        else:
            # TODO: handle labels for labeled but unshaped arrays
            return RangeResult(range="List[" + dtype + "]")

        items = []
        if dmin is not None:
            items.append(f"min_length={dmin}")
        if dmax is not None:
            items.append(f"max_length={dmax}")

        items.append(f"item_type={dtype}")
        items = ", ".join(items)
        range = f"conlist({items})"

        return RangeResult(range=range, imports=_ConListImports)

    def any_shape(self, array: Optional[ArrayExpression] = None, with_inner_union: bool = False) -> RangeResult:
        """
        An AnyShaped array (using :class:`.AnyShapeArray` )

        Args:
            array (:class:`.ArrayExpression`): The array expression (not used)
            with_inner_union (bool): If ``True`` , the innermost type is a ``Union`` of the ``AnyShapeArray`` class
                and ``dtype`` (default: ``False`` )

        """
        if self.dtype in ("Any", "AnyType"):
            range = "AnyShapeArray"
        else:
            range = f"AnyShapeArray[{self.dtype}]"

        if with_inner_union:
            range = f"Union[{range}, {self.dtype}]"
        return RangeResult(range=range, injected_classes=_AnyShapeArrayInjects, imports=_AnyShapeArrayImports)

    def bounded_dimensions(self, array: ArrayExpression) -> RangeResult:
        """
        A nested series of ``List[]`` ranges with :attr:`.dtype` at the center.

        When an array expression allows for a range of dimensions, each set of ``List`` s is joined by a ``Union`` .
        """
        if array.exact_number_dimensions or (
            array.minimum_number_dimensions
            and array.maximum_number_dimensions
            and array.minimum_number_dimensions == array.maximum_number_dimensions
        ):
            exact_dims = array.exact_number_dimensions or array.minimum_number_dimensions
            return RangeResult(range=self._list_of_lists(exact_dims, self.dtype))
        elif not array.maximum_number_dimensions and (
            array.minimum_number_dimensions is None or array.minimum_number_dimensions == 1
        ):
            return self.any_shape()
        elif array.maximum_number_dimensions:
            # e.g., if min = 2, max = 3, range = Union[List[List[dtype]], List[List[List[dtype]]]]
            min_dims = array.minimum_number_dimensions if array.minimum_number_dimensions is not None else 1
            ranges = [self._list_of_lists(i, self.dtype) for i in range(min_dims, array.maximum_number_dimensions + 1)]
            # TODO: Format this nicely!
            return RangeResult(range="Union[" + ", ".join(ranges) + "]")
        else:
            # min specified with no max
            # e.g., if min = 3, range = List[List[AnyShapeArray[dtype]]]
            return RangeResult(
                range=self._list_of_lists(array.minimum_number_dimensions - 1, self.any_shape().range),
                injected_classes=_AnyShapeArrayInjects,
                imports=_AnyShapeArrayImports,
            )

    def parameterized_dimensions(self, array: ArrayExpression) -> RangeResult:
        """
        Constrained shapes using :func:`pydantic.conlist`

        TODO:
        - preservation of aliases
        - (what other metadata is allowable on labeled dimensions?)
        """
        # generate dimensions from inside out and then format
        # e.g., if dimensions = [{min_card: 3}, {min_card: 2}],
        # range = conlist(min_length=3, item_type=conlist(min_length=2, item_type=dtype))
        range = self.dtype
        for dimension in reversed(array.dimensions):
            range = self._parameterized_dimension(dimension, range).range

        return RangeResult(range=range, imports=_ConListImports)

    def complex_dimensions(self, array: ArrayExpression) -> RangeResult:
        """
        Mixture of parameterized dimensions with a max or min (or both) shape for anonymous dimensions.

        A mixture of ``List`` , :class:`.conlist` , and :class:`.AnyShapeArray` .
        """
        # first process any unlabeled dimensions which must be the innermost level of the range,
        # then wrap that with labeled dimensions
        if array.exact_number_dimensions or (
            array.minimum_number_dimensions
            and array.maximum_number_dimensions
            and array.minimum_number_dimensions == array.maximum_number_dimensions
        ):
            exact_dims = array.exact_number_dimensions or array.minimum_number_dimensions
            if exact_dims > len(array.dimensions):
                res = RangeResult(range=self._list_of_lists(exact_dims - len(array.dimensions), self.dtype))
            elif exact_dims == len(array.dimensions):
                # equivalent to labeled shape
                return self.parameterized_dimensions(array)
            else:
                raise ValueError(
                    "if exact_number_dimensions is provided, it must be greater than the parameterized dimensions"
                )

        elif array.maximum_number_dimensions is not None and not array.maximum_number_dimensions:
            # unlimited n dimensions, so innermost is AnyShape with dtype
            res = self.any_shape(with_inner_union=True)

            if array.minimum_number_dimensions and array.minimum_number_dimensions > len(array.dimensions):
                # some minimum anonymous dimensions but unlimited max dimensions
                # e.g., if min = 3, len(dim) = 2, then res.range = List[Union[AnyShapeArray[dtype], dtype]]
                # res.range will be wrapped with the 2 labeled dimensions later
                res.range = self._list_of_lists(array.minimum_number_dimensions - len(array.dimensions), res.range)

        elif array.minimum_number_dimensions and array.maximum_number_dimensions is None:
            raise ValueError(
                (
                    "Cannot specify a minimum_number_dimensions while maximum is None while using labeled dimensions - "
                    "either use exact_number_dimensions > len(dimensions) for extra parameterized dimensions or set "
                    "maximum_number_dimensions explicitly to False for unbounded dimensions"
                )
            )
        elif array.maximum_number_dimensions:
            initial_min = array.minimum_number_dimensions if array.minimum_number_dimensions is not None else 0
            dmin = max(len(array.dimensions), initial_min) - len(array.dimensions)
            dmax = array.maximum_number_dimensions - len(array.dimensions)

            res = self.bounded_dimensions(
                ArrayExpression(minimum_number_dimensions=dmin, maximum_number_dimensions=dmax)
            )
        else:
            raise ValueError("Unsupported array specification! this is almost certainly a bug!")  # pragma: no cover

        # Wrap inner dimension with labeled dimension
        # e.g., if dimensions = [{min_card: 3}, {min_card: 2}]
        # and res.range = List[Union[AnyShapeArray[dtype], dtype]]
        # (min 3 dims, no max dims)
        # then the final range = conlist(
        #     min_length=3,
        #     item_type=conlist(
        #         min_length=2,
        #         item_type=List[Union[AnyShapeArray[dtype], dtype]]
        #     )
        # )
        for dim in reversed(array.dimensions):
            res = res.merge(self._parameterized_dimension(dim, dtype=res.range))

        return res


class NPTypingArray(ArrayRangeGenerator):
    """
    Represent array range with nptyping, and serialization/loading with an ArrayProxy
    """

    REPR = ArrayRepresentation.NPARRAY

    def __init__(self, **kwargs):
        super(self).__init__(**kwargs)
        raise NotImplementedError("NPTyping array ranges are not implemented yet :(")
