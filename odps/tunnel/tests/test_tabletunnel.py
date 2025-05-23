#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 1999-2025 Alibaba Group Holding Ltd.
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

from __future__ import print_function

import json
import math
import random
import sys
import time
import warnings
from collections import OrderedDict
from datetime import date, datetime
from multiprocessing.pool import ThreadPool

import six

try:
    from string import letters
except ImportError:
    from string import ascii_letters as letters
try:
    import zoneinfo
except ImportError:
    zoneinfo = None
try:
    import pytz
except ImportError:
    pytz = None

import mock
import pytest
import requests

from ... import options, types
from ...compat import DECIMAL_TYPES, ConfigParser, Decimal, Monthdelta, Version
from ...errors import DatetimeOverflowError, throw_if_parsable
from ...models import Record, TableSchema
from ...rest import RestClient
from ...tests.core import (
    approx_list,
    flaky,
    get_code_mode,
    get_test_unique_name,
    odps2_typed_case,
    pandas_case,
    py_and_c,
    pyarrow_case,
    tn,
)
from ...utils import get_zone_name, to_binary, to_text
from .. import TableTunnel
from ..errors import TunnelReadTimeout, TunnelWriteTimeout
from ..tabletunnel import BaseTableTunnelSession

_TUNNEL_VERSION_NO_METRICS = 5


@pytest.fixture(autouse=True)
def check_malicious_requests(odps):
    def _new_throw_if_parsable(resp, *args, **kw):
        if resp.status_code in (400, 404) and not resp.url.startswith(odps.endpoint):
            try:
                throw_if_parsable(resp, *args, **kw)
            except:
                raise AssertionError("Malicious request detected.")
        else:
            throw_if_parsable(resp, *args, **kw)

    with mock.patch("odps.errors.throw_if_parsable", new=_new_throw_if_parsable):
        yield


class TunnelTestUtil(object):
    def __init__(self, odps, tunnel):
        self.odps = odps
        self.last_table = None
        self.tunnel = tunnel

    def _gen_random_bigint(self):
        return random.randint(*types.bigint._bounds)

    def _gen_random_string(self, max_length=15):
        gen_letter = lambda: letters[random.randint(0, 51)]
        return to_text("".join([gen_letter() for _ in range(random.randint(1, 15))]))

    def _gen_random_double(self):
        return random.uniform(-(2**32), 2**32)

    def _gen_random_datetime(self):
        return datetime.fromtimestamp(random.randint(0, int(time.time())))

    def _gen_random_boolean(self):
        return random.uniform(-1, 1) > 0

    def _gen_random_decimal(self):
        return Decimal(str(self._gen_random_double()))

    def _gen_random_array_type(self):
        t = random.choice(["string", "bigint", "double", "boolean"])
        return types.Array(t)

    gen_random_array_type = _gen_random_array_type

    def _gen_random_map_type(self):
        random_key_type = random.choice(["bigint", "string"])
        random_value_type = random.choice(["bigint", "string", "double"])

        return types.Map(random_key_type, random_value_type)

    def _gen_random_array(self, random_type, size=None):
        size = size or random.randint(100, 500)

        random_type = types.validate_data_type(random_type)
        if isinstance(random_type, types.Array):
            random_type = random_type.value_type
        method = getattr(self, "_gen_random_%s" % random_type.name)
        array = [method() for _ in range(size)]

        return array

    gen_random_array = _gen_random_array

    def _gen_random_map(self, random_map_type):
        size = random.randint(100, 500)

        random_map_type = types.validate_data_type(random_map_type)

        key_arrays = self.gen_random_array(random_map_type.key_type, size)
        value_arrays = self.gen_random_array(random_map_type.value_type, size)

        m = OrderedDict(zip(key_arrays, value_arrays))
        return m

    def gen_table(
        self,
        partition=None,
        partition_type=None,
        partition_val=None,
        size=100,
        odps=None,
        append_partitions=True,
    ):
        def gen_name(name):
            if "<" in name:
                name = name.split("<", 1)[0]
            if len(name) > 4:
                name = name[:4]
            else:
                name = name[:2]
            return name

        odps = odps or self.odps
        test_table_name = tn("pyodps_t_tmp_tunnel_" + get_test_unique_name())

        types = ["bigint", "string", "double", "datetime", "boolean", "decimal"]
        types.append(self.gen_random_array_type().name)
        types.append(self._gen_random_map_type().name)
        random.shuffle(types)
        names = [gen_name(t) for t in types]

        odps.delete_table(test_table_name, if_exists=True)
        partition_names = [partition] if partition else None
        partition_types = [partition_type] if partition_type else None
        table = self.last_table = odps.create_table(
            test_table_name,
            TableSchema.from_lists(
                names,
                types,
                partition_names=partition_names,
                partition_types=partition_types,
            ),
            lifecycle=1,
        )
        if partition_val:
            table.create_partition("%s=%s" % (partition, partition_val))

        data = []
        for i in range(size):
            record = []
            for t in types:
                n = t.split("<", 1)[0]
                method = getattr(self, "_gen_random_" + n)
                if n in ("map", "array"):
                    record.append(method(t))
                elif n == "double" and i == 0:
                    record.append(float("nan"))
                else:
                    record.append(method())
            if (
                append_partitions
                and partition is not None
                and partition_val is not None
            ):
                record.append(partition_val)
            data.append(record)

        return table, data

    def assert_reads_data_equal(self, reads, data, ignore_tz=True):
        def _nan_inf(v):
            return v if not isinstance(v, float) or not math.isnan(v) else float("-inf")

        reads = [r.values if isinstance(r, Record) else r for r in reads]
        data = [r.values if isinstance(r, Record) else r for r in data]

        sortable_types = (
            six.string_types,
            six.integer_types,
            bool,
            float,
            DECIMAL_TYPES,
            datetime,
            date,
        )
        sort_idxes = []
        if reads and data:
            for idx, v in enumerate(data[0]):
                if isinstance(v, sortable_types):
                    sort_idxes.append(idx)
            if sort_idxes:
                reads = sorted(
                    reads, key=lambda x: tuple(_nan_inf(x[ix]) for ix in sort_idxes)
                )
                data = sorted(
                    data, key=lambda x: tuple(_nan_inf(x[ix]) for ix in sort_idxes)
                )
        try:
            assert len(data) == len(reads)
            for val1, val2 in zip(data, reads):
                assert len(val1) == len(val2)
                for it1, it2 in zip(val1, val2):
                    if isinstance(it1, dict):
                        assert len(it1) == len(it2)
                        assert any(it1[k] == it2[k] for k in it1) is True
                    elif isinstance(it1, list):
                        assert it1 == list(it2)
                    elif (
                        isinstance(it1, float)
                        and math.isnan(it1)
                        and isinstance(it2, float)
                        and math.isnan(it2)
                    ):
                        continue
                    else:
                        assert it1 == it2
        except AssertionError:
            print("sort_idxes =", repr(sort_idxes))
            print("reads =", repr(reads), file=sys.stderr)
            print("data =", repr(data), file=sys.stderr)
            raise

    @classmethod
    def _check_metrics(cls, metrics):
        assert metrics is not None
        repr(metrics)
        assert metrics.network_wall_cost > 0
        assert metrics.client_process_cost > 0
        assert metrics.tunnel_process_cost > 0
        assert metrics.storage_cost >= 0

    def upload_data(
        self, test_table, records, compress=False, check_metrics=False, **kw
    ):
        tunnel = kw.pop("tunnel", self.tunnel)
        upload_ss = tunnel.create_upload_session(test_table, **kw)
        # make sure session reprs work well
        repr(upload_ss)
        writer = upload_ss.open_record_writer(0, compress=compress)

        # test use right py or c writer
        assert get_code_mode() == writer._mode()

        for r in records:
            record = upload_ss.new_record()
            # test record
            assert get_code_mode() == record._mode()
            for i, it in enumerate(r):
                record[i] = it
            writer.write(record)
        writer.close()
        if check_metrics:
            self._check_metrics(writer.metrics)
        upload_ss.commit([0])

    def stream_upload_data(self, test_table, records, compress=False, **kw):
        upload_ss = self.tunnel.create_stream_upload_session(test_table, **kw)
        # make sure session reprs work well
        repr(upload_ss)
        writer = upload_ss.open_record_writer(compress=compress)

        # test use right py or c writer
        assert get_code_mode() == writer._mode()

        for r in records:
            record = upload_ss.new_record()
            # test record
            assert get_code_mode() == record._mode()
            for i, it in enumerate(r):
                record[i] = it
            writer.write(record)
        writer.close()
        upload_ss.abort()

    def buffered_upload_data(
        self,
        test_table,
        records,
        buffer_size=None,
        compress=False,
        check_metrics=False,
        **kw
    ):
        upload_ss = self.tunnel.create_upload_session(test_table, **kw)
        # make sure session reprs work well
        repr(upload_ss)
        writer = upload_ss.open_record_writer(
            buffer_size=buffer_size, compress=compress
        )
        for r in records:
            record = upload_ss.new_record()
            for i, it in enumerate(r):
                record[i] = it
            writer.write(record)
        writer.close()

        if check_metrics:
            self._check_metrics(writer.metrics)

        if buffer_size is None:
            assert len(writer.get_blocks_written()) == 1
        else:
            assert len(writer.get_blocks_written()) > 1
        upload_ss.commit(writer.get_blocks_written())

    def download_data(
        self,
        test_table,
        compress=False,
        columns=None,
        append_partitions=None,
        check_metrics=False,
        **kw
    ):
        count = kw.pop("count", None)
        download_ss = self.tunnel.create_download_session(test_table, **kw)
        count = count or download_ss.count
        # make sure session reprs work well
        repr(download_ss)
        down_kw = (
            {"append_partitions": append_partitions}
            if append_partitions is not None
            else {}
        )
        with download_ss.open_record_reader(
            0, count, compress=compress, columns=columns, **down_kw
        ) as reader:
            # test use right py or c writer
            assert get_code_mode() == reader._mode()

            records = []

            for record in reader:
                records.append(tuple(record.values))
                assert get_code_mode() == record._mode()

        if check_metrics:
            self._check_metrics(reader.metrics)
        return records

    def gen_data(self):
        return [
            (
                "hello \x00\x00 world",
                2**63 - 1,
                math.pi,
                datetime(2015, 9, 19, 2, 11, 25, 33000),
                True,
                Decimal("3.14"),
                ["simple", "easy"],
                OrderedDict({"s": 1}),
            ),
            (
                "goodbye",
                222222,
                math.e,
                datetime(2020, 3, 10),
                False,
                Decimal("1234567898765431"),
                ["true", None],
                OrderedDict({"true": 1}),
            ),
            (
                "c" * 300,
                -(2**63) + 1,
                -2.222,
                datetime(1999, 5, 25, 3, 10),
                True,
                Decimal(28318318318318318),
                ["false"],
                OrderedDict({"false": 0}),
            ),
            (
                "c" * 20,
                -(2**11) + 1,
                2.222,
                datetime(1961, 10, 30, 11, 32),
                True,
                Decimal("12345678.98765431"),
                ["true"],
                OrderedDict({"false": 0}),
            ),
        ]

    def create_table(self, table_name, odps=None):
        fields = ["id", "int_num", "float_num", "dt", "bool", "dec", "arr", "m"]
        types = [
            "string",
            "bigint",
            "double",
            "datetime",
            "boolean",
            "decimal",
            "array<string>",
            "map<string,bigint>",
        ]

        odps = odps or self.odps
        odps.delete_table(table_name, if_exists=True)
        return odps.create_table(
            table_name, TableSchema.from_lists(fields, types), lifecycle=1
        )

    def create_partitioned_table(self, table_name, odps=None):
        fields = ["id", "int_num", "float_num", "dt", "bool", "dec", "arr", "m"]
        types = [
            "string",
            "bigint",
            "double",
            "datetime",
            "boolean",
            "decimal",
            "array<string>",
            "map<string,bigint>",
        ]

        odps = odps or self.odps
        odps.delete_table(table_name, if_exists=True)
        return odps.create_table(
            table_name,
            TableSchema.from_lists(fields, types, ["ds"], ["string"]),
        )

    def delete_table(self, table_name):
        self.odps.delete_table(table_name)


def _reloader():
    from ...conftest import get_config

    cfg = get_config()
    cfg.tunnel = TableTunnel(cfg.odps, endpoint=cfg.odps._tunnel_endpoint)


py_and_c_deco = py_and_c(
    [
        "odps.models.record",
        "odps.models",
        "odps.tunnel.io.reader",
        "odps.tunnel.io.writer",
        "odps.tunnel.tabletunnel",
        "odps.tunnel.instancetunnel",
    ],
    _reloader,
)


@pytest.fixture
def setup(odps, tunnel):
    random.seed(0)
    raw_chunk_size = options.chunk_size
    raw_buffer_size = options.tunnel.block_buffer_size

    util = TunnelTestUtil(odps, tunnel)
    try:
        yield util
    finally:
        if util.last_table:
            util.last_table.drop(async_=True)
        options.chunk_size = raw_chunk_size
        options.tunnel.block_buffer_size = raw_buffer_size


@pytest.fixture
def config_metrics(request):
    orig_get_common_header = BaseTableTunnelSession.get_common_headers
    raw_enable_metrics = options.tunnel.enable_client_metrics

    def patched_get_common_headers(self, *args, **kw):
        headers = orig_get_common_header(*args, **kw)
        headers["x-odps-tunnel-version"] = str(_TUNNEL_VERSION_NO_METRICS)
        return headers

    try:
        options.tunnel.enable_client_metrics = request.param
        if not request.param:
            with mock.patch(
                "odps.tunnel.tabletunnel.BaseTableTunnelSession.get_common_headers",
                new=patched_get_common_headers,
            ):
                yield request.param
        else:
            yield request.param
    finally:
        options.tunnel.enable_client_metrics = raw_enable_metrics


def test_malicious_request_detection(setup):
    with pytest.raises(AssertionError):
        setup.tunnel.tunnel_rest.get(
            setup.tunnel.tunnel_rest.endpoint + "/logview/host"
        )


@py_and_c_deco
@pytest.mark.parametrize("config_metrics", [False, True], indirect=True)
def test_upload_and_download_by_raw_tunnel(setup, config_metrics):
    test_table_name = tn("pyodps_test_raw_tunnel_" + get_test_unique_name(5))
    setup.create_table(test_table_name)
    data = setup.gen_data()

    setup.upload_data(
        test_table_name, data, tags="elephant", check_metrics=config_metrics
    )
    records = setup.download_data(
        test_table_name, tags="elephant", check_metrics=config_metrics
    )
    assert list(data) == list(records)

    setup.delete_table(test_table_name)


@py_and_c_deco
@odps2_typed_case
def test_upload_and_download_wrapped_strings(odps):
    test_table_name = tn("pyodps_test_wrapped_string_" + get_test_unique_name(5))
    odps.delete_table(test_table_name, if_exists=True)
    odps.create_table(test_table_name, "col1 string, col2 binary")

    class U(str if six.PY3 else unicode):  # noqa: F821
        pass

    class B(bytes if six.PY3 else str):
        pass

    tunnel = TableTunnel(odps)
    sess = tunnel.create_upload_session(test_table_name)
    with sess.open_record_writer(0) as writer:
        writer.write(sess.new_record([U("a"), B(b"b")]))
    sess.commit([0])

    sess = tunnel.create_download_session(test_table_name)
    with sess.open_record_reader(0, 1) as reader:
        assert [list(r.values) for r in reader] == [["a", b"b"]]

    odps.delete_table(test_table_name, if_exists=True)


@py_and_c_deco
@pytest.mark.parametrize("allow_schema_mismatch", [False, True])
def test_stream_upload_and_download_tunnel(odps, setup, allow_schema_mismatch):
    test_table_name = tn("pyodps_test_stream_upload_" + get_test_unique_name(5))
    odps.delete_table(test_table_name, if_exists=True)
    setup.create_table(test_table_name)
    data = setup.gen_data()

    setup.stream_upload_data(
        test_table_name, data, allow_schema_mismatch=allow_schema_mismatch
    )
    records = setup.download_data(test_table_name)
    assert list(data) == list(records)

    setup.delete_table(test_table_name)


@py_and_c_deco
@pytest.mark.parametrize("config_metrics", [False, True], indirect=True)
def test_buffered_upload_and_download_by_raw_tunnel(setup, config_metrics):
    table, data = setup.gen_table(size=10)
    setup.buffered_upload_data(table, data, check_metrics=config_metrics)
    records = setup.download_data(table, check_metrics=config_metrics)
    setup.assert_reads_data_equal(records, data)
    setup.delete_table(table)

    table, data = setup.gen_table(size=10)
    setup.buffered_upload_data(
        table, data, buffer_size=1024, check_metrics=config_metrics
    )
    records = setup.download_data(table, check_metrics=config_metrics)
    setup.assert_reads_data_equal(records, data)
    setup.delete_table(table)


@py_and_c_deco
@pytest.mark.skipif(pytz is None and zoneinfo is None, reason="pytz not installed")
@pytest.mark.parametrize("zone", [False, True, "Asia/Shanghai", "America/Los_Angeles"])
def test_buffered_upload_and_download_with_timezone(setup, zone):
    from ...utils import MillisecondsConverter

    try:
        tz = MillisecondsConverter._get_tz(zone)
        options.local_timezone = zone
        table, data = setup.gen_table(size=10)
        setup.buffered_upload_data(table, data)
        records = setup.download_data(table, count=10)

        if not isinstance(zone, bool):
            for idx, rec in enumerate(records):
                new_rec = []
                for d in rec:
                    if not isinstance(d, datetime):
                        new_rec.append(d)
                        continue
                    assert get_zone_name(d.tzinfo) == get_zone_name(tz)
                    new_rec.append(d.replace(tzinfo=None))
                records[idx] = tuple(new_rec)

        setup.assert_reads_data_equal(records, data)
        setup.delete_table(table)
    finally:
        options.local_timezone = None


@py_and_c_deco
def test_download_with_specified_columns(setup):
    test_table_name = tn("pyodps_test_raw_tunnel_columns_" + get_code_mode())
    setup.create_table(test_table_name)

    data = setup.gen_data()
    setup.upload_data(test_table_name, data)

    records = setup.download_data(test_table_name, columns=["id"])
    assert [r[0] for r in records] == [r[0] for r in data]
    for r in records:
        for i in range(1, len(r)):
            assert r[i] is None
    setup.delete_table(test_table_name)


@py_and_c_deco
def test_download_limitation(odps, setup):
    old_limit = options.table_read_limit

    test_table_name = tn("pyodps_test_tunnel_limit_" + get_code_mode())
    setup.create_table(test_table_name)
    data = setup.gen_data()
    setup.upload_data(test_table_name, data * 20)

    options.table_read_limit = 10
    with pytest.warns(RuntimeWarning):
        records = setup.download_data(test_table_name, compress=True, count=20)
    assert len(records) == 10

    options.table_read_limit = None
    with warnings.catch_warnings(record=True) as warning_records:
        warnings.simplefilter("always")
        records = setup.download_data(test_table_name, compress=True, count=20)
        assert len(records) == 20
    assert len(warning_records) == 0

    options.table_read_limit = old_limit


@py_and_c_deco
def test_partition_upload_and_download_by_raw_tunnel(odps, setup):
    test_table_name = tn("pyodps_test_raw_partition_tunnel_" + get_code_mode())
    test_table_partition = "ds=test"
    odps.delete_table(test_table_name, if_exists=True)

    table = setup.create_partitioned_table(test_table_name)
    table.create_partition(test_table_partition)
    data = setup.gen_data()

    setup.upload_data(test_table_name, data, partition_spec=test_table_partition)
    records = setup.download_data(test_table_name, partition_spec=test_table_partition)
    assert list(data) == [r[:-1] for r in records]

    records = setup.download_data(
        test_table_name, partition_spec=test_table_partition, append_partitions=False
    )
    assert list(data) == list(records)

    setup.delete_table(test_table_name)


@py_and_c_deco
def test_partition_download_with_specified_columns(odps, setup):
    test_table_name = tn("pyodps_test_raw_tunnel_partition_columns_" + get_code_mode())
    test_table_partition = "ds=test"
    odps.delete_table(test_table_name, if_exists=True)

    table = setup.create_partitioned_table(test_table_name)
    table.create_partition(test_table_partition)
    data = setup.gen_data()

    setup.upload_data(test_table_name, data, partition_spec=test_table_partition)
    records = setup.download_data(
        test_table_name, partition_spec=test_table_partition, columns=["int_num"]
    )
    assert [r[1] for r in data] == [r[0] for r in records]

    setup.delete_table(test_table_name)


@py_and_c_deco
@pytest.mark.parametrize(
    "algo, module",
    [(None, None), ("snappy", "snappy"), ("zstd", "zstandard"), ("lz4", "lz4.frame")],
)
def test_upload_and_download_with_compress(setup, algo, module):
    raw_chunk_size = options.chunk_size
    options.chunk_size = 16
    if module:
        pytest.importorskip(module)

    try:
        test_table_name = tn("pyodps_test_zlib_tunnel_" + get_test_unique_name(5))
        setup.create_table(test_table_name)
        data = setup.gen_data()

        setup.upload_data(test_table_name, data, compress=True, compress_algo=algo)
        records = setup.download_data(
            test_table_name, compress=True, compress_algo=algo
        )
        assert list(data) == list(records)

        setup.delete_table(test_table_name)
    finally:
        options.chunk_size = raw_chunk_size


@py_and_c_deco
def test_buffered_upload_and_download_by_zlib_tunnel(setup):
    table, data = setup.gen_table(size=10)
    setup.buffered_upload_data(table, data, compress=True)
    records = setup.download_data(table, compress=True)
    setup.assert_reads_data_equal(records, data)
    setup.delete_table(table)


@py_and_c_deco
def test_table_upload_and_download_tunnel(odps, setup):
    table, data = setup.gen_table()

    records = [table.new_record(values=d) for d in data]
    odps.write_table(table, 0, records)

    reads = list(odps.read_table(table, len(data)))
    for val1, val2 in zip(data, [r.values for r in reads]):
        for it1, it2 in zip(val1, val2):
            if isinstance(it1, dict):
                assert len(it1) == len(it2)
                assert any(it1[k] == it2[k] for k in it1) is True
            elif isinstance(it1, list):
                assert it1 == list(it2)
            else:
                if (
                    isinstance(it1, float)
                    and math.isnan(it1)
                    and isinstance(it2, float)
                    and math.isnan(it2)
                ):
                    continue
                assert it1 == it2

    table.drop()


@py_and_c_deco
def test_multi_table_upload_and_download_tunnel(odps, setup):
    table, data = setup.gen_table(size=10)

    records = [table.new_record(values=d) for d in data]

    odps.write_table(table, 0, records[:5])
    odps.write_table(table, 0, records[5:])

    reads = list(odps.read_table(table, len(data)))
    setup.assert_reads_data_equal(reads, data)


@py_and_c_deco
def test_parallel_table_upload_and_download_tunnel(odps, setup):
    p = "ds=test"

    table, data = setup.gen_table(
        partition=p.split("=", 1)[0],
        partition_type="string",
        partition_val=p.split("=", 1)[1],
    )
    assert table.exist_partition(p) is True
    records = [table.new_record(values=d) for d in data]

    n_blocks = 5
    blocks = list(range(n_blocks))
    n_threads = 2
    thread_pool = ThreadPool(n_threads)

    def gen_block_records(block_id):
        c = len(data)
        st = int(c / n_blocks * block_id)
        if block_id < n_blocks - 1:
            ed = int(c / n_blocks * (block_id + 1))
        else:
            ed = c
        return records[st:ed]

    def write(w):
        def inner(arg):
            idx, r = arg
            w.write(idx, r)

        return inner

    with table.open_writer(partition=p, blocks=blocks) as writer:
        thread_pool.map(write(writer), [(i, gen_block_records(i)) for i in blocks])

    for step in range(1, 4):
        reads = []
        expected = []

        with table.open_reader(partition=p) as reader:
            count = reader.count

            for i in range(n_blocks):
                start = int(count / n_blocks * i)
                if i < n_blocks - 1:
                    end = int(count / n_blocks * (i + 1))
                else:
                    end = count
                for record in reader[start:end:step]:
                    reads.append(record.values)
                expected.extend(data[start:end:step])

        setup.assert_reads_data_equal(expected, reads)

    table.drop()


@odps2_typed_case
@py_and_c_deco
def test_primitive_types2(odps):
    table_name = tn("test_hivetunnel_singleton_types_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(
        table_name,
        "col1 tinyint, col2 smallint, col3 int, col4 float, col5 binary",
        lifecycle=1,
    )
    assert table.table_schema.types == [
        types.tinyint,
        types.smallint,
        types.int_,
        types.float_,
        types.binary,
    ]

    contents = [
        [127, 32767, 1234321, 10.5432, b"Hello, world!"],
        [-128, -32768, 4312324, 20.1234, b"Excited!"],
        [-1, 10, 9875479, 20.1234, b"Bravo!"],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [list(v.values) for v in written]
    assert approx_list(contents) == values

    table.drop(if_exists=True)


@py_and_c_deco
@odps2_typed_case
def test_date(odps):
    table_name = tn("test_hivetunnel_date_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(table_name, "col1 int, col2 date", lifecycle=1)
    assert table.table_schema.types == [types.int_, types.date]

    contents = [[0, date(2020, 2, 12)], [1, date(1900, 1, 1)], [2, date(2000, 3, 20)]]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [list(v.values) for v in written]
    assert contents == values

    table.drop(if_exists=True)


@py_and_c_deco
@pandas_case
@odps2_typed_case
def test_timestamp(odps):
    import pandas as pd

    table_name = tn("test_hivetunnel_timestamp_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(table_name, "col1 int, col2 timestamp", lifecycle=1)
    assert table.table_schema.types == [types.int_, types.timestamp]

    contents = [
        [0, pd.Timestamp("2013-09-21 11:23:35.196045321")],
        [1, pd.Timestamp("1998-02-15 23:59:21.943829154")],
        [2, pd.Timestamp("2017-10-31 00:12:39.396583106")],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [list(v.values) for v in written]
    assert contents == values

    table.drop(if_exists=True)


@py_and_c_deco
@pandas_case
def test_pandas_na(odps):
    import pandas as pd

    if not hasattr(pd, "NA"):
        pytest.skip("Need pandas>1.0 to run this test")

    table_name = tn("test_pandas_na_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(table_name, "col1 bigint, col2 string", lifecycle=1)
    contents = [
        [0, "agdesfdr"],
        [1, pd.NA],
        [pd.NA, "aetlkakls;dfj"],
        [3, "aetlkakls;dfj"],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [[x if x is not None else pd.NA for x in v.values] for v in written]
    assert contents == values

    table.drop(if_exists=True)


@py_and_c_deco
@odps2_typed_case
def test_length_limit_types(odps):
    table_name = tn("test_hivetunnel_length_limit_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(
        table_name, "col1 int, col2 varchar(20), col3 char(30)", lifecycle=1
    )
    assert table.table_schema.types[0] == types.int_
    assert isinstance(table.table_schema.types[1], types.Varchar)
    assert table.table_schema.types[1].size_limit == 20
    assert isinstance(table.table_schema.types[2], types.Char)
    assert table.table_schema.types[2].size_limit == 30

    contents = [
        [0, "agdesfdr", "sadfklaslkjdvvn"],
        [1, "sda;fkd", "asdlfjjls;admc"],
        [2, "aetlkakls;dfj", "sadffafafsafsaf"],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))

    contents = [r[:2] + [r[2] + " " * (30 - len(r[2]))] for r in contents]
    values = [list(v.values) for v in written]
    assert contents == values

    table.drop(if_exists=True)


@py_and_c_deco
@odps2_typed_case
def test_decimal2(odps):
    table_name = tn("test_hivetunnel_decimal_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)

    table = odps.create_table(
        table_name,
        "col1 int, col2 decimal(6,2), col3 decimal(10), col4 decimal(10,3)",
        lifecycle=1,
    )
    assert table.table_schema.types[0] == types.int_
    assert isinstance(table.table_schema.types[1], types.Decimal)
    # comment out due to behavior change of ODPS SQL
    # self.assertIsNone(table.table_schema.types[1].precision)
    # self.assertIsNone(table.table_schema.types[1].scale)
    assert isinstance(table.table_schema.types[2], types.Decimal)
    assert table.table_schema.types[2].precision == 10
    assert isinstance(table.table_schema.types[3], types.Decimal)
    assert table.table_schema.types[3].precision == 10
    assert table.table_schema.types[3].scale == 3

    contents = [
        [0, Decimal("2.34"), Decimal("34567"), Decimal("56.789")],
        [1, Decimal("11.76"), Decimal("9321"), Decimal("19.125")],
        [2, Decimal("134.21"), Decimal("1642"), Decimal("999.214")],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [list(v.values) for v in written]
    assert contents == values

    table.drop(if_exists=True)


@py_and_c_deco
@pandas_case
@odps2_typed_case
def test_intervals(odps):
    import pandas as pd

    empty_table_name = tn("test_hivetunnel_interval_empty_" + get_code_mode())
    odps.delete_table(empty_table_name, if_exists=True)
    empty_table = odps.create_table(empty_table_name, "col1 int", if_not_exists=True)

    table_name = tn("test_hivetunnel_interval_io_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)
    odps.execute_sql(
        "create table %s lifecycle 1 as\n"
        "select interval_day_time('2 1:2:3') as col1,"
        "  interval_year_month('10-11') as col2\n"
        "from %s" % (table_name, empty_table_name)
    )
    table = odps.get_table(table_name)
    assert table.table_schema.types == [
        types.interval_day_time,
        types.interval_year_month,
    ]

    contents = [
        [pd.Timedelta(seconds=1048576, nanoseconds=428571428), Monthdelta(13)],
        [pd.Timedelta(seconds=934567126, nanoseconds=142857142), Monthdelta(-20)],
        [pd.Timedelta(seconds=91230401, nanoseconds=285714285), Monthdelta(50)],
    ]
    odps.write_table(table_name, contents)
    written = list(odps.read_table(table_name))
    values = [list(v.values) for v in written]
    assert contents == values

    table.drop()
    empty_table.drop()


@py_and_c_deco
@odps2_typed_case
@pytest.mark.parametrize(
    "struct_as_dict, use_ordered_dict", [(False, None), (True, False), (True, True)]
)
def test_struct(odps, struct_as_dict, use_ordered_dict):
    table_name = tn("test_hivetunnel_struct_io_" + get_test_unique_name(5))
    odps.delete_table(table_name, if_exists=True)

    try:
        options.struct_as_dict = struct_as_dict
        options.struct_as_ordered_dict = use_ordered_dict

        col_def = (
            "col1 int, col2 struct<name:string,age:int,"
            "parents:map<varchar(20),smallint>,hobbies:array<varchar(100)>>"
        )
        table = odps.create_table(table_name, col_def, lifecycle=1)
        assert table.table_schema.types[0] == types.int_
        struct_type = table.table_schema.types[1]
        assert isinstance(struct_type, types.Struct)

        contents = [
            [0, ("user1", 20, {"fa": 5, "mo": 6}, ["worship", "yacht"])],
            [1, ("user2", 65, {"fa": 2, "mo": 7}, ["ukelele", "chess"])],
            [2, ("user3", 32, {"fa": 1, "mo": 3}, ["poetry", "calligraphy"])],
        ]
        if struct_as_dict:
            dict_hook = OrderedDict if use_ordered_dict else dict
            for c in contents:
                c[1] = dict_hook(zip(struct_type.field_types.keys(), c[1]))
        else:
            contents[-1][1] = struct_type.namedtuple_type(*contents[-1][1])

        odps.write_table(table_name, contents)
        written = list(odps.read_table(table_name))
        values = [list(v.values) for v in written]
        assert contents == values

        table.drop(if_exists=True)
    finally:
        options.struct_as_dict = False
        options.struct_as_ordered_dict = None


@py_and_c_deco
def test_async_table_upload_and_download(odps, setup):
    table, data = setup.gen_table()

    records = [table.new_record(values=d) for d in data]
    odps.write_table(table, 0, records)

    reads = list(odps.read_table(table, len(data), async_mode=True))
    setup.assert_reads_data_equal(reads, data)

    table.drop()


def test_write_timeout(odps, setup):
    table, data = setup.gen_table()

    def _patched(*_, **__):
        raise requests.ConnectionError("timed out")

    with mock.patch("odps.rest.RestClient.put", new=_patched):
        with pytest.raises(TunnelWriteTimeout) as ex_info:
            records = [table.new_record(values=d) for d in data]
            odps.write_table(table, 0, records)

        assert isinstance(ex_info.value, requests.ConnectionError)

    table.drop()


@py_and_c_deco
@odps2_typed_case
def test_decimal_with_complex_types(odps):
    table_name = tn("test_decimal_with_complex_types_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)
    table = odps.create_table(
        table_name,
        "col1 array<decimal(38, 18)>, col3 struct<d: decimal(38,18)>",
        lifecycle=1,
    )

    try:
        data_to_write = [[[Decimal("12.345"), Decimal("18.41")], (Decimal("514.321"),)]]
        with table.open_writer() as writer:
            writer.write(data_to_write)

        with table.open_reader() as reader:
            records = [list(rec.values) for rec in reader]

        assert data_to_write == records
    finally:
        table.drop()


@py_and_c_deco
@odps2_typed_case
@pandas_case
def test_json_timestamp_types(odps):
    import pandas as pd

    table_name = tn("test_json_types_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)
    hints = {"odps.sql.type.json.enable": "true"}
    table = odps.create_table(
        table_name, "col1 json, col2 timestamp_ntz, col3 string", hints=hints
    )

    try:
        ts_value = pd.Timestamp("2023-07-16 12:10:11.234156231")
        data_to_write = [[{"root": {"key": "value"}}, ts_value, "hello"]]
        with table.open_writer() as writer:
            writer.write(data_to_write)
        with table.open_reader() as reader:
            records = [list(rec.values) for rec in reader]
        assert data_to_write == records
    finally:
        table.drop()


@py_and_c_deco
def test_antique_datetime(odps):
    table_name = tn("test_datetime_overflow_table_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)
    table = odps.create_table(table_name, "col datetime", lifecycle=1)

    options.allow_antique_date = False
    options.tunnel.overflow_date_as_none = False
    try:
        odps.execute_sql(
            "INSERT INTO %s(col) VALUES (cast('1900-01-01 00:00:00' as datetime))"
            % table_name
        )
        with pytest.raises(DatetimeOverflowError):
            with table.open_reader() as reader:
                _ = next(reader)

        options.allow_antique_date = True
        with table.open_reader(reopen=True) as reader:
            rec = next(reader)
            assert rec[0].year == 1900

        table.truncate()
        odps.execute_sql(
            "INSERT INTO %s(col) VALUES (cast('0000-01-01 00:00:00' as datetime))"
            % table_name
        )
        with pytest.raises(DatetimeOverflowError):
            with table.open_reader() as reader:
                _ = next(reader)

        options.tunnel.overflow_date_as_none = True
        with table.open_reader(reopen=True) as reader:
            rec = next(reader)
            assert rec[0] is None
    finally:
        options.allow_antique_date = False
        options.tunnel.overflow_date_as_none = False

        table.drop()


@py_and_c_deco
def test_tunnel_read_with_retry(odps, setup):
    # must be imported here as odps.tunnel.tabletunnel will
    # be reloaded by the decorator
    from ..tabletunnel import TableDownloadSession

    test_table_name = tn("pyodps_t_tmp_tunnel_read_with_retry_" + get_code_mode())
    odps.delete_table(test_table_name, if_exists=True)

    table = odps.create_table(test_table_name, "col string")

    try:
        data = [["str%d" % idx] for idx in range(10)]
        with table.open_writer() as writer:
            writer.write(data)

        ranges = []
        original = TableDownloadSession._build_input_stream

        def new_build_input_stream(self, start, count, *args, **kw):
            ranges.append((start, count))
            assert start in (0, 2)
            assert start == 0 or count == session.count - 2
            return original(self, start, count, *args, **kw)

        with mock.patch(
            "odps.tunnel.tabletunnel.TableDownloadSession._build_input_stream",
            new=new_build_input_stream,
        ):
            tunnel = TableTunnel(odps)
            session = tunnel.create_download_session(table)
            reader = session.open_record_reader(0, session.count)

            reader._inject_error(2, ValueError)
            result = [r.values for r in reader]

        assert ranges == [(0, 10), (2, 8)]
        setup.assert_reads_data_equal(result, data)
    finally:
        table.drop()


@pyarrow_case
def test_tunnel_preview_table_simple_types(odps, setup):
    import pyarrow as pa

    tunnel = TableTunnel(odps)
    test_table_name = tn("pyodps_test_tunnel_preview_table_simple_types")

    odps.delete_table(test_table_name, if_exists=True)
    table = setup.create_partitioned_table(test_table_name, odps=odps)
    with tunnel.open_preview_reader(
        table, limit=3, arrow=False, tags="elephant"
    ) as reader:
        records = list(reader)
        assert len(records) == 0

    data = setup.gen_data()
    table.create_partition("ds=test")
    setup.upload_data(test_table_name, data, tunnel=tunnel, partition_spec="ds=test")

    try:
        options.struct_as_dict = True

        # test arrow result on limited column types
        if Version(pa.__version__) < Version("1.0"):
            # pyarrow < 1.0 crashes under compression
            kw = {}
        else:
            kw = {"compress_algo": "zstd"}

        with tunnel.open_preview_reader(
            table, limit=2, columns=["id", "int_num", "float_num"], arrow=True, **kw
        ) as reader:
            arrow_table = reader.read()
        result_rows = [tuple(x.as_py() for x in tp) for tp in zip(*arrow_table.columns)]
        assert result_rows == [tp[:3] for tp in data[:2]]

        with tunnel.open_preview_reader(table, limit=3, arrow=False) as reader:
            records = list(reader)
        result_rows = [tuple(rec.values) for rec in records]
        assert result_rows == [tp + ("test",) for tp in data[:3]]
    finally:
        options.struct_as_dict = False

    odps.delete_table(test_table_name)


@pandas_case
@pyarrow_case
@odps2_typed_case
def test_tunnel_preview_odps_extended_datetime(odps):
    import pandas as pd

    tunnel = TableTunnel(odps)

    test_table_name = tn("pyodps_test_tunnel_preview_odps_extended_types")
    odps.delete_table(test_table_name, if_exists=True)
    odps.execute_sql(
        "create table " + test_table_name + " as "
        "select cast('2023-10-12 11:05:11.123451231' as timestamp) as ts_col, "
        "interval '3 12:30:11.134512345' day to second as intv_col"
    )
    table = odps.get_table(test_table_name)

    try:
        with tunnel.open_preview_reader(table, arrow=False) as reader:
            record = list(reader)[0]
        assert record["ts_col"] == pd.Timestamp("2023-10-12 11:05:11.123451231")
        assert record["intv_col"] == pd.Timedelta(
            days=3,
            hours=12,
            minutes=30,
            seconds=11,
            microseconds=134512,
            nanoseconds=345,
        )
    finally:
        odps.delete_table(test_table_name)


@pyarrow_case
@py_and_c_deco
def test_tunnel_preview_legacy_decimal(odps):
    tunnel = TableTunnel(odps)

    test_table_name = tn(
        "pyodps_test_tunnel_preview_odps_legacy_decimal_" + get_code_mode()
    )
    odps.delete_table(test_table_name, if_exists=True)

    values = [
        None,
        Decimal("0.0"),
        Decimal("0.571428"),
        Decimal("21.3456"),
        Decimal("-5678"),
        Decimal("134567234121.561345671892"),
        Decimal("-453462321413.2345643456336"),
    ]

    try:
        options.sql.settings = {"odps.sql.decimal.odps2": "false"}
        table = odps.create_table(test_table_name, "col decimal")

        with table.open_writer() as writer:
            writer.write([[v] for v in values])
        with tunnel.open_preview_reader(table, arrow=False) as reader:
            written = [rec[0] for rec in reader]
        assert values == written
    finally:
        options.sql.settings = {}
        odps.delete_table(test_table_name)


@pandas_case
@pyarrow_case
@odps2_typed_case
@pytest.mark.parametrize("struct_as_dict", [False, True])
def test_tunnel_preview_table_complex_types(odps, struct_as_dict):
    import pandas as pd

    tunnel = TableTunnel(odps)
    test_table_name = tn(
        "pyodps_test_tunnel_preview_table_complex_types_" + get_test_unique_name(5)
    )
    odps.delete_table(test_table_name, if_exists=True)

    table = odps.create_table(
        test_table_name,
        "col1 decimal(10, 2), col2 timestamp, col3 map<string, array<bigint>>, "
        "col4 array<map<string, bigint>>, col5 struct<key: string, value: map<string, bigint>>",
        lifecycle=1,
    )
    data = [
        [
            Decimal("1234.52"),
            pd.Timestamp("2023-07-15 23:08:12.134567123"),
            {"abcd": [1234, None], "egh": [5472]},
            [{"uvw": 123, "xyz": 567}, {"abcd": 345}],
            ("this_key", {"pqr": 47, "st": 56}),
        ],
        [
            Decimal("5473.12"),
            pd.Timestamp("2023-07-16 10:01:23.345673214"),
            {"uvw": [9876, None], "tre": [3421]},
            [{"mrp": 342, "vcs": 165}],
            ("other_key", {"df": 12, "das": 27}),
        ],
    ]
    with table.open_writer() as writer:
        writer.write(data)

    try:
        options.struct_as_dict = struct_as_dict

        with tunnel.open_preview_reader(table, arrow=False) as reader:
            records = list(reader)
        result_rows = [tuple(rec.values) for rec in records]
        if struct_as_dict:
            for r in data:
                r[-1] = OrderedDict(zip(["key", "value"], r[-1]))
        assert [tuple(r) for r in data] == result_rows
    finally:
        options.struct_as_dict = False
    table.drop()


@flaky(max_runs=3)
@py_and_c_deco
def test_upsert_table(odps):
    table_name = tn("test_upsert_table_" + get_code_mode())
    odps.delete_table(table_name, if_exists=True)
    table = odps.create_table(
        table_name,
        "key string not null, value string",
        transactional=True,
        primary_key="key",
        lifecycle=1,
    )

    tunnel = TableTunnel(odps, endpoint=odps._tunnel_endpoint)

    try:
        upsert_session = tunnel.create_upsert_session(table)
        stream = upsert_session.open_upsert_stream(compress=True)
        rec = upsert_session.new_record(["0", "v1"])
        stream.upsert(rec)
        rec = upsert_session.new_record(["0", "v2"])
        stream.upsert(rec)
        rec = upsert_session.new_record(["0", "v3"])
        stream.upsert(rec)
        rec = upsert_session.new_record(["1", "v1"])
        stream.upsert(rec)
        rec = upsert_session.new_record(["2", "v1"])
        stream.upsert(rec)
        stream.delete(rec)
        stream.flush()
        stream.close()

        upsert_session.commit()

        inst = odps.execute_sql("SELECT * FROM %s" % table_name)
        with inst.open_reader() as reader:
            records = [list(rec.values) for rec in reader]
        assert sorted(records) == [["0", "v3"], ["1", "v1"]]
    finally:
        table.drop()


def test_table_tunnel_with_quota(odps_with_tunnel_quota, config):
    from odps.rest import RestClient

    orig_request = RestClient.request

    def patch_request(self, *args, **kw):
        if self._endpoint == tunnel_endpoint:
            assert kw["params"]["quotaName"] == quota_name
        return orig_request(self, *args, **kw)

    odps = odps_with_tunnel_quota
    table_name = tn("test_table_tunnel_with_quota")
    odps.delete_table(table_name, if_exists=True)

    quota_name = config.get("test", "default_tunnel_quota_name")
    tunnel = TableTunnel(odps, quota_name=quota_name)
    tunnel_endpoint = tunnel.tunnel_rest.endpoint
    tb = odps.create_table(table_name, "col1 string", lifecycle=1)

    with mock.patch("odps.rest.RestClient.request", new=patch_request):
        upload_session = tunnel.create_upload_session(tb)
        assert upload_session.quota_name == quota_name
        upload_session.reload()
        assert upload_session.quota_name == quota_name

        writer = upload_session.open_record_writer()
        writer.write(tb.new_record(["data"]))
        writer.close()
        upload_session.commit(writer.get_blocks_written())

        download_session = tunnel.create_download_session(tb)
        assert download_session.quota_name == quota_name
        download_session.reload()
        assert download_session.quota_name == quota_name

        reader = download_session.open_record_reader(0, 1)
        assert list(reader)[0][0] == "data"

    tb.drop()


@py_and_c_deco
def test_read_write_long_binary(odps_with_long_string):
    odps = odps_with_long_string

    data_len = 16 * 1024**2
    maxsize_prop = (
        int(odps.get_project().get_property("odps.sql.cfile2.field.maxsize", None) or 0)
        * 1024
    )
    if data_len > maxsize_prop:
        pytest.skip("maxsize on project %s not configured." % odps.project)

    test_str = "abcd" * (data_len // 4)

    test_table_name = tn("pyodps_t_tmp_long_binary_test_" + get_code_mode())
    odps.delete_table(test_table_name, if_exists=True)

    table = odps.create_table(test_table_name, "col1 string", lifecycle=1)

    try:
        rec = table.new_record([test_str])
        assert rec[0] == test_str

        rec = table.new_record()
        rec[0] = test_str
        assert rec[0] == test_str

        with table.open_writer() as writer:
            writer.write([test_str])
        with table.open_reader() as reader:
            assert next(reader)[0] == test_str
    finally:
        table.drop()


def test_read_secondary_project_table(config, odps):
    try:
        secondary_project = config.get("test", "secondary_project")
    except ConfigParser.NoOptionError:
        pytest.skip("secondary_project not configured.")

    table_name = tn("test_secondary_project_table")
    odps.delete_table(table_name, project=secondary_project, if_exists=True)
    table = odps.create_table(
        table_name, "col1 string", project=secondary_project, lifecycle=1
    )
    try:
        with table.open_writer() as writer:
            writer.write([["test_data"]])

        # method 1: access with project argument
        table_tunnel = TableTunnel(odps, project=secondary_project)
        down_sess = table_tunnel.create_download_session(table_name)
        with down_sess.open_record_reader(0, down_sess.count) as reader:
            records = list(reader)
            assert records[0][0] == "test_data"

        # method 2: access with full table name
        table_tunnel = TableTunnel(odps)
        down_sess = table_tunnel.create_download_session(
            secondary_project + "." + table_name
        )
        with down_sess.open_record_reader(0, down_sess.count) as reader:
            records = list(reader)
            assert records[0][0] == "test_data"
    finally:
        table.drop()


def test_async_mode_timeout(odps):
    table_name = tn("test_async_mode_timeout_table")
    odps.delete_table(table_name, if_exists=True)
    table = odps.create_table(table_name, "col1 string", lifecycle=1)

    old_request = RestClient.request

    def new_request(self, *args, **kw):
        headers = kw.get("headers") or {}
        resp = old_request(self, *args, **kw)
        if "x-odps-tunnel-version" not in headers or (
            "downloads" not in resp.url and "downloadid" not in resp.url
        ):
            return resp
        assert "asyncmode=true" in resp.url or "downloads=" not in resp.url
        parsed = json.loads(resp.content)
        parsed["Status"] = "initiating"
        resp._content = to_binary(json.dumps(parsed))
        return resp

    try:
        with mock.patch("odps.rest.RestClient.request", new=new_request), pytest.raises(
            TunnelReadTimeout
        ):
            table_tunnel = TableTunnel(odps)
            table_tunnel.create_download_session(table, timeout=10)
    finally:
        table.drop()
