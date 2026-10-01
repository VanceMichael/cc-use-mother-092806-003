"""领域对象的 JSON 序列化。

所有实体统一编码为 ``{"__type__": 类名, ...字段}``，枚举取其字符串值，
集合标记为 ``{"__set__": [...]}``。仓储快照与恢复都经过这里，
不依赖第三方库。
"""

from __future__ import annotations

import dataclasses
import types
import typing
from enum import Enum
from typing import Any, Union, get_args, get_origin

from . import model

_REGISTRY: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        model.Company,
        model.User,
        model.Order,
        model.Batch,
        model.BatchException,
        model.Location,
        model.RailWindow,
        model.Meter,
        model.Reading,
        model.MeterGap,
        model.CarbonFactor,
        model.AllocationLine,
        model.Allocation,
        model.Dispute,
        model.Alert,
    )
}

_SET_MARK = "__set__"
_TYPE_MARK = "__type__"


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        payload: dict[str, Any] = {_TYPE_MARK: type(value).__name__}
        for field in dataclasses.fields(value):
            payload[field.name] = _encode(getattr(value, field.name))
        return payload
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, set):
        return {_SET_MARK: [_encode(item) for item in sorted(value)]}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    raise TypeError(f"不支持序列化的类型: {type(value)!r}")


def _resolve(hint: Any, payload: Any) -> Any:
    """按类型提示把已解码的 payload 还原为目标类型。"""
    origin = get_origin(hint)

    if origin in (Union, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if payload is None:
            return None
        if len(args) == 1:
            return _resolve(args[0], payload)
        # 多个非空候选：携带类型标记的是 dataclass，其余按原始值处理
        return payload
    if origin in (list, tuple):
        item_hint = get_args(hint)[0]
        items = [_resolve(item_hint, item) for item in payload]
        return tuple(items) if origin is tuple else items
    if origin is set:
        item_hint = get_args(hint)[0]
        return {_resolve(item_hint, item) for item in payload[_SET_MARK]}
    if origin is dict:
        value_hint = get_args(hint)[1]
        return {key: _resolve(value_hint, item) for key, item in payload.items()}
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(payload)
    if dataclasses.is_dataclass(hint):
        return _decode(payload)
    return payload


def _decode(payload: Any) -> Any:
    if not isinstance(payload, dict) or _TYPE_MARK not in payload:
        return payload
    cls = _REGISTRY[payload[_TYPE_MARK]]
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name in payload:
            kwargs[field.name] = _resolve(hints[field.name], payload[field.name])
    return cls(**kwargs)


def dumps_object(value: Any) -> dict[str, Any]:
    return _encode(value)


def loads_object(payload: dict[str, Any]) -> Any:
    return _decode(payload)


def snapshot_to_json(collections: dict[str, list[Any]]) -> dict[str, Any]:
    """把各实体集合导出为可写盘的纯 JSON 结构。"""
    return {name: [_encode(item) for item in items] for name, items in collections.items()}


def snapshot_from_json(data: dict[str, Any]) -> dict[str, list[Any]]:
    return {name: [_decode(item) for item in items] for name, items in data.items()}
