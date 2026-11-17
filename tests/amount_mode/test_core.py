import dataclasses
import json
import random
from pathlib import Path

import pytest

from number_app.amount_mode.formatting import format_amount_group
from number_app.amount_mode.input_config import (
    build_request_bytes, canonical_json, load_config, load_json_strict, parse_config, validate_request,
)
from number_app.amount_mode.partition import generate_group_sizes, legal_group_sizes
from number_app.amount_mode.pool import iter_groups, shuffle_item_ids
from number_app.amount_mode.spec import AmountBatchSpec

FIXTURE = json.loads((Path(__file__).parents[2] / "按套金额随机小组_验收样例.json").read_text(encoding="utf-8"))


def test_fixture_version_and_all_manual_bodies():
    assert FIXTURE["fixture_version"] == 2 and FIXTURE["handoff_version"] == "1.1"
    for case in FIXTURE["cases"]:
        records = [{**row, "amount": int(row["amount"])} for row in case["records"]]
        body = format_amount_group(records)
        assert body == case["expected_body"], case["name"]
        assert sum(row["amount"] for row in records) == int(case["expected_group_amount"])
        assert len({row["code"] for row in records}) == case["expected_distinct_codes"]
        assert len(records) == case["expected_raw_count"]


def test_all_complete_input_expectations_and_file_roundtrip(tmp_path):
    for index, case in enumerate(FIXTURE["complete_input_examples"]):
        config = {"set_count": case["set_count"], "amounts": case["amounts"]}
        target = tmp_path / f"input{index}.json"
        target.write_bytes(canonical_json(config))
        spec = load_config(target)
        assert spec.raw_total == case["expected_raw_total"]
        assert str(spec.per_code_amount) == case["expected_per_code_amount"]
        assert str(spec.total_amount) == case["expected_total_amount"]
        assert (spec.raw_total + 4) // 5 == case["expected_group_count_min"]
        assert spec.raw_total // 2 == case["expected_group_count_max"]


def test_all_invalid_configs_and_json():
    for case in FIXTURE["invalid_examples"]:
        with pytest.raises(ValueError, match=".+"):
            parse_config({"set_count": case["set_count"], "amounts": case["amounts"]})
    for case in FIXTURE["invalid_json_inputs"]:
        with pytest.raises(ValueError, match=".+"):
            parse_config(case["raw_json"])
    for value in ([], (1.0,), (True,), (0,), (-1,), (), (10 ** 100,), tuple([1] * 301)):
        with pytest.raises(ValueError):
            AmountBatchSpec(value)
    for amount in ("01", "1" * 101, 1, True):
        with pytest.raises(ValueError):
            parse_config({"set_count": 1, "amounts": [amount]})


def test_request_canonical_identity_and_snapshot_are_immutable():
    draft = [1, 5, 2]
    spec = AmountBatchSpec(tuple(draft))
    data = build_request_bytes(spec, batch_id="test-batch", created_at="2026-11-17T11:00:00+08:00")
    draft[2] = 9
    assert spec.amounts == (1, 5, 2)
    assert spec.per_code_amount == 8
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.amounts = (1,)
    obj = load_json_strict(data)
    assert validate_request(obj) == spec
    assert data == canonical_json(obj) and data.endswith(b"\n") and b"\r" not in data
    assert obj["sets"] == [{"set_no": 1, "amount": "1"}, {"set_no": 2, "amount": "5"}, {"set_no": 3, "amount": "2"}]
    for sets in ([obj["sets"][1], obj["sets"][0], obj["sets"][2]],
                 [{"set_no": True, "amount": "1"}, *obj["sets"][1:]],
                 [obj["sets"][0], obj["sets"][0], obj["sets"][2]]):
        with pytest.raises(ValueError):
            validate_request({**obj, "sets": sets})
    for key, value in (("schema_version", True), ("schema_version", 3), ("mode", "old"),
                       ("created_at", "2026-11-17T11:00:00"), ("batch_id", "../bad")):
        with pytest.raises(ValueError):
            validate_request({**obj, key: value})


def test_every_tail_choice_2_through_7_reaches_exact_zero():
    expected = {2: (2,), 3: (3,), 4: (2, 4), 5: (2, 3, 5), 6: (2, 3, 4), 7: (2, 3, 4, 5)}
    def walk(remaining):
        if remaining == 0:
            return
        for size in legal_group_sizes(remaining):
            assert size in (2, 3, 4, 5) and remaining - size != 1
            walk(remaining - size)
    for remaining in range(2, 8):
        assert legal_group_sizes(remaining) == expected[remaining]
        walk(remaining)
        for seed in (0, 3, 21):
            sizes = generate_group_sizes(remaining, random.Random(seed))
            assert sum(sizes) == remaining and all(2 <= q <= 5 for q in sizes)


def test_complete_pool_partition_and_identity_amount_binding():
    for set_count in (1, 3, 300):
        amounts = tuple(1 + index % 7 for index in range(set_count))
        spec = AmountBatchSpec(amounts)
        ids = shuffle_item_ids(spec.raw_total, random.Random(300 + set_count))
        sizes = generate_group_sizes(spec.raw_total, random.Random(73 + set_count))
        assert sorted(ids) == list(range(spec.raw_total))
        assert sum(sizes) == spec.raw_total
        assert (spec.raw_total + 4) // 5 <= len(sizes) <= spec.raw_total // 2
        raw_counts, amounts_per_code = [0] * 1000, [0] * 1000
        consumed = 0
        for group in iter_groups(ids, sizes):
            assert 2 <= len(group) <= 5
            consumed += len(group)
            for item_id in group:
                code = item_id % 1000
                raw_counts[code] += 1
                amounts_per_code[code] += amounts[item_id // 1000]
        assert consumed == spec.raw_total
        assert raw_counts == [set_count] * 1000
        assert amounts_per_code == [spec.per_code_amount] * 1000


def test_published_digit_limit_does_not_limit_derived_amounts():
    maximum = 10 ** 100 - 1
    spec = AmountBatchSpec((maximum, maximum))
    records = [{"item_id": 7, "set_no": 1, "code": "007", "amount": maximum},
               {"item_id": 1007, "set_no": 2, "code": "007", "amount": maximum}]
    assert format_amount_group(records) == f"007直选{2 * maximum}元\n"
    assert len(str(spec.per_code_amount)) == 101
    assert len(str(spec.total_amount)) == 104


def test_invalid_formatter_records_and_partition_input_are_rejected():
    valid = [{"item_id": 7, "set_no": 1, "code": "007", "amount": 1},
             {"item_id": 128, "set_no": 1, "code": "128", "amount": 1}]
    for change in ({"amount": True}, {"amount": "1"}, {"code": "7"}, {"item_id": True},
                   {"set_no": 2}, {"code": "１２８"}):
        with pytest.raises(ValueError):
            format_amount_group([valid[0], {**valid[1], **change}])
    with pytest.raises(ValueError):
        format_amount_group([valid[0], valid[0]])
    for value in (1, 0, True, 3.0, 300001):
        with pytest.raises(ValueError):
            generate_group_sizes(value)
    with pytest.raises(ValueError):
        list(iter_groups([0, 1, 2], [2]))
