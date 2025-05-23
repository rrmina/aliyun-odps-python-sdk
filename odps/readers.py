#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 1999-2024 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import csv
import itertools
import math
from collections import OrderedDict

from requests import Response

from . import compat, options, types, utils
from .compat import StringIO, six
from .models.record import Record


class AbstractRecordReader(object):
    def __iter__(self):
        return self

    def __next__(self):
        raise NotImplementedError

    next = __next__

    @classmethod
    def _calc_count(cls, start, end, step):
        if end is None:
            return end
        step = step or 1
        return int(math.ceil(float(end - start) / step))

    @classmethod
    def _get_slice(cls, item):
        if isinstance(item, six.integer_types):
            start = item
            end = start + 1
            step = 1
        elif isinstance(item, slice):
            start = item.start or 0
            end = item.stop
            step = item.step or 1
        else:
            raise ValueError("Reader only supports index and slice operation.")

        return start, end, step

    def __getitem__(self, item):
        start, end, step = self._get_slice(item)
        count = self._calc_count(start, end, step)

        if start < 0 or (count is not None and count <= 0) or step < 0:
            raise ValueError("start, count, or step cannot be negative")

        it = self._get_slice_iter(start=start, end=end, step=step)
        if isinstance(item, six.integer_types):
            try:
                return next(it)
            except StopIteration:
                raise IndexError("Index out of range: %s" % item)
        return it

    def _get_slice_iter(self, start=None, end=None, step=None):
        class SliceIterator(six.Iterator):
            def __init__(self, it):
                self.it = it

            def __iter__(self):
                return self.it

            def __next__(self):
                return next(self.it)

            @staticmethod
            def to_pandas():
                if end is not None:
                    count = (end - (start or 0)) // (step or 1)
                else:
                    count = None
                pstep = None if step == 1 else step
                kw = dict(start=start, count=count, step=pstep)
                kw = {k: v for k, v in kw.items() if v is not None}
                return parent.to_pandas(**kw)

        parent = self
        return SliceIterator(self._iter(start=start, end=end, step=step))

    def _iter(self, start=None, end=None, step=None):
        start = start or 0
        step = step or 1
        curr = start

        for _ in range(start):
            try:
                next(self)
            except StopIteration:
                return

        while True:
            for i in range(step):
                try:
                    record = next(self)
                except StopIteration:
                    return
                if i == 0:
                    yield record
                curr += 1
                if end is not None and curr >= end:
                    return

    def _data_to_result_frame(
        self, data, unknown_as_string=True, as_type=None, columns=None
    ):
        from .df.backends.frame import ResultFrame
        from .df.backends.odpssql.types import (
            odps_schema_to_df_schema,
            odps_type_to_df_type,
        )

        kw = dict()
        if getattr(self, "schema", None) is not None:
            kw["schema"] = odps_schema_to_df_schema(self.schema)
        elif getattr(self, "_schema", None) is not None:
            # do not remove as there might be coverage missing
            kw["schema"] = odps_schema_to_df_schema(self._schema)

        column_names = columns or getattr(self, "_column_names", None)
        if column_names is not None:
            self._columns = [self.schema[c] for c in column_names]
        if getattr(self, "_columns", None) is not None:
            cols = []
            for col in self._columns:
                col = copy.copy(col)
                col.type = odps_type_to_df_type(col.type)
                cols.append(col)
            kw["columns"] = cols

        if hasattr(self, "raw"):
            try:
                import pandas as pd

                from .df.backends.pd.types import pd_to_df_schema

                data = pd.read_csv(StringIO(self.raw))
                schema = kw["schema"] = pd_to_df_schema(
                    data, unknown_as_string=unknown_as_string, as_type=as_type
                )
                columns = kw.pop("columns", None)
                if columns and len(columns) < len(schema):
                    sel_cols = [c.name for c in self._columns]
                    data = data[sel_cols]
                    kw["schema"] = types.OdpsSchema(columns)
            except (ImportError, ValueError):
                pass

        if not kw:
            raise ValueError(
                "Cannot convert to ResultFrame from %s." % type(self).__name__
            )

        return ResultFrame(data, **kw)

    def to_result_frame(
        self,
        unknown_as_string=True,
        as_type=None,
        start=None,
        count=None,
        columns=None,
        **iter_kw
    ):
        read_row_batch_size = options.tunnel.read_row_batch_size
        if "end" in iter_kw:
            end = iter_kw["end"]
        else:
            end = (
                None
                if count is None
                else (start or 0) + count * (iter_kw.get("step") or 1)
            )

        frames = []
        if hasattr(self, "raw"):
            # data represented as raw csv: just skip iteration
            data = [r for r in self._iter(start=start, end=end, **iter_kw)]
        else:
            offset_iter = itertools.cycle(compat.irange(read_row_batch_size))
            data = [None] * read_row_batch_size
            for offset, rec in zip(
                offset_iter, self._iter(start=start, end=end, **iter_kw)
            ):
                data[offset] = rec
                if offset != read_row_batch_size - 1:
                    continue

                frames.append(
                    self._data_to_result_frame(
                        data, unknown_as_string=unknown_as_string, as_type=as_type
                    )
                )
                data = [None] * read_row_batch_size
                if len(frames) > options.tunnel.batch_merge_threshold:
                    frames = [frames[0].concat(*frames[1:])]

        if not frames or data[0] is not None:
            data = list(itertools.takewhile(lambda x: x is not None, data))
            frames.append(
                self._data_to_result_frame(
                    data,
                    unknown_as_string=unknown_as_string,
                    as_type=as_type,
                    columns=columns,
                )
            )
        return frames[0].concat(*frames[1:])

    def to_pandas(self, start=None, count=None, **kw):
        import pandas  # noqa: F401

        return self.to_result_frame(start=start, count=count, **kw).values


class CsvRecordReader(AbstractRecordReader):
    NULL_TOKEN = "\\N"
    BACK_SLASH_ESCAPE = "\\x%02x" % ord("\\")

    def __init__(self, schema, stream, **kwargs):
        # shift csv field limit size to match table field size
        max_field_size = kwargs.pop("max_field_size", 0) or types.String._max_length
        if csv.field_size_limit() < max_field_size:
            csv.field_size_limit(max_field_size)

        self._schema = schema
        self._csv_columns = None
        self._fp = stream
        if isinstance(self._fp, Response):
            self.raw = self._fp.content if six.PY2 else self._fp.text
        else:
            self.raw = self._fp

        if options.tunnel.string_as_binary:
            self._csv = csv.reader(six.StringIO(self._escape_csv_bin(self.raw)))
        else:
            self._csv = csv.reader(six.StringIO(self._escape_csv(self.raw)))

        self._filtered_col_names = (
            set(x.lower() for x in kwargs["columns"]) if "columns" in kwargs else None
        )
        self._columns = None
        self._filtered_col_idxes = None

    @classmethod
    def _escape_csv(cls, s):
        escaped = utils.to_text(s).encode("unicode_escape")
        # Make invisible chars available to `csv` library.
        # Note that '\n' and '\r' should be unescaped.
        # '\\' should be replaced with '\x5c' before unescaping
        # to avoid mis-escaped strings like '\\n'.
        return (
            utils.to_text(escaped)
            .replace("\\\\", cls.BACK_SLASH_ESCAPE)
            .replace("\\n", "\n")
            .replace("\\r", "\r")
        )

    @classmethod
    def _escape_csv_bin(cls, s):
        escaped = utils.to_binary(s).decode("latin1").encode("unicode_escape")
        # Make invisible chars available to `csv` library.
        # Note that '\n' and '\r' should be unescaped.
        # '\\' should be replaced with '\x5c' before unescaping
        # to avoid mis-escaped strings like '\\n'.
        return (
            utils.to_text(escaped)
            .replace("\\\\", cls.BACK_SLASH_ESCAPE)
            .replace("\\n", "\n")
            .replace("\\r", "\r")
        )

    @staticmethod
    def _unescape_csv(s):
        return s.encode("utf-8").decode("unicode_escape")

    @staticmethod
    def _unescape_csv_bin(s):
        return s.encode("utf-8").decode("unicode_escape").encode("latin1")

    def _readline(self):
        try:
            values = next(self._csv)
            res = []

            read_binary = options.tunnel.string_as_binary
            if read_binary:
                unescape_csv = self._unescape_csv_bin
            else:
                unescape_csv = self._unescape_csv

            for i, value in enumerate(values):
                value = unescape_csv(value)
                if value == self.NULL_TOKEN:
                    res.append(None)
                elif self._csv_columns and self._csv_columns[i].type == types.boolean:
                    if value == "true":
                        res.append(True)
                    elif value == "false":
                        res.append(False)
                    else:
                        res.append(value)
                elif self._csv_columns and isinstance(
                    self._csv_columns[i].type, types.Map
                ):
                    col_type = self._csv_columns[i].type
                    if not (value.startswith("{") and value.endswith("}")):
                        raise ValueError("Dict format error!")

                    items = []
                    for kv in value[1:-1].split(","):
                        k, v = kv.split(":", 1)
                        k = col_type.key_type.cast_value(k.strip(), types.string)
                        v = col_type.value_type.cast_value(v.strip(), types.string)
                        items.append((k, v))
                    res.append(OrderedDict(items))
                elif self._csv_columns and isinstance(
                    self._csv_columns[i].type, types.Array
                ):
                    col_type = self._csv_columns[i].type
                    if not (value.startswith("[") and value.endswith("]")):
                        raise ValueError("Array format error!")

                    items = []
                    for item in value[1:-1].split(","):
                        item = col_type.value_type.cast_value(
                            item.strip(), types.string
                        )
                        items.append(item)
                    res.append(items)
                else:
                    res.append(value)
            return res
        except StopIteration:
            return

    def __next__(self):
        self._load_columns()

        values = self._readline()
        if values is None or len(values) == 0:
            raise StopIteration

        if self._filtered_col_idxes:
            values = [values[idx] for idx in self._filtered_col_idxes]
        return Record(self._columns, values=values)

    next = __next__

    def read(self, start=None, count=None, step=None):
        if count is None:
            end = None
        else:
            start = start or 0
            step = step or 1
            end = start + count * step
        return self._iter(start=start, end=end, step=step)

    def _load_columns(self):
        if self._csv_columns is not None:
            return

        values = self._readline()
        self._csv_columns = []
        for value in values:
            if self._schema is None:
                self._csv_columns.append(types.Column(name=value, typo="string"))
            else:
                if self._schema.is_partition(value):
                    self._csv_columns.append(self._schema.get_partition(value))
                else:
                    self._csv_columns.append(self._schema.get_column(value))

        if self._csv_columns is not None and self._filtered_col_names:
            self._filtered_col_idxes = []
            self._columns = []
            for idx, col in enumerate(self._csv_columns):
                if col.name.lower() in self._filtered_col_names:
                    self._filtered_col_idxes.append(idx)
                    self._columns.append(col)
        else:
            self._columns = self._csv_columns

    def to_pandas(self, start=None, count=None, **kw):
        kw.pop("n_process", None)
        return super(CsvRecordReader, self).to_pandas(start=start, count=count, **kw)

    def close(self):
        if hasattr(self._fp, "close"):
            self._fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# make class name compatible
RecordReader = CsvRecordReader
