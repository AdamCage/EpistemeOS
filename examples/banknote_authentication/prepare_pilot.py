"""Prepare a short Banknote research pilot without starting a model or a study."""

from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zipfile import ZipFile

from prepare_capture import MAX_SOURCE_BYTES, SOURCE_URL, prepare_capture


ARCHIVE_URL = "https://archive.ics.uci.edu/static/public/267/banknote%2Bauthentication.zip"
ARCHIVE_SHA256 = "1e2acd9a2085fadf3d8145c12d3d22af853320d52294a6590c2eaf75fdc05227"
SOURCE_SHA256 = "d0539aaed2139ba7a587b3e34fb345ce503ff7d5d33dbf9912d8e195ce425cb9"
SOURCE_NAME = "data_banknote_authentication.txt"
HYPOTHESIS = (
    "На фиксированном holdout опубликованного UCI Banknote Authentication "
    "логистическая регрессия по variance и skewness точнее классификатора, "
    "который всегда предсказывает класс большинства в train.\n"
)
GROUPED_HYPOTHESIS = (
    "На фиксированном holdout опубликованного UCI Banknote Authentication, "
    "с одной строкой на точную пару преобразованных признаков, логистическая регрессия "
    "по variance и skewness точнее классификатора, который предсказывает класс большинства в train.\n"
)
AUTHOR_REPLY = (
    "Ценность пилота — проверить, может ли EpistemeOS получить и ограничить "
    "воспроизводимый вывод о фиксированном контрасте на опубликованных данных. "
    "Критерий — парное сравнение точности логистической регрессии и большинства "
    "train по заранее заданному правилу с парным интервалом и тестом Мак-Немара. "
    "При неопределённом или отрицательном результате сохраняем исход и ограничения. "
    "Набор публичен и уже использовался для классификации. Проверяем область "
    "совпадения с prior art и ограничиваем вывод одной групповой разбивкой. "
    "Новая выборка и независимый научный review не входят в этот пилот.\n"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download() -> bytes:
    request = Request(ARCHIVE_URL, headers={"User-Agent": "EpistemeOS-banknote/1",
                                           "Accept-Encoding": "identity"})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10) as response:
        if response.status != 200 or response.geturl() != ARCHIVE_URL:
            raise ValueError("UCI did not return the pinned archive URL")
        archive = response.read(MAX_SOURCE_BYTES + 1)
    if len(archive) > MAX_SOURCE_BYTES or sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("UCI archive does not match the pinned SHA-256 and size limit")
    with ZipFile(BytesIO(archive)) as source:
        member = source.getinfo(SOURCE_NAME)
        if member.file_size > MAX_SOURCE_BYTES:
            raise ValueError("UCI archive member exceeds its size limit")
        with source.open(member) as stream:
            return stream.read(MAX_SOURCE_BYTES + 1)


def pilot_config(model: str, reasoning_effort: str, *, grouping: dict | None = None) -> dict:
    if not model or model != model.strip() or any(ord(char) < 32 for char in model):
        raise ValueError("model must be an explicit Codex catalog slug")
    config = dict(
        budget=dict(stages=1, attempts=8, model_calls=24, literature_calls=2,
                    wall_seconds=1800, operation_seconds=120),
        scope={
            "dataset": "Lohweg (2012), UCI Banknote Authentication, DOI 10.24432/C55P57, CC BY 4.0.",
            "population": "Only rows of one publicly released banknote dataset and one fixed train/holdout split.",
            "exposure": "Public historical dataset. The two features, split rule and baseline are fixed for this pilot.",
            "estimand": "Paired accuracy difference, logistic regression minus training majority, on the fixed holdout.",
            "null": "The paired accuracy difference does not show a positive advantage under the frozen pack rule.",
            "prior_art": "Classification of this dataset is published. Literature observations scope novelty questions.",
            "evidence": "Pack confirmation is mechanical. Same-data reanalysis cannot establish new-sample replication.",
            "decision": "Preserve positive, negative and inconclusive results and stop after the single frozen cohort.",
        },
        candidates=[dict(
            label="banknote-fixed-holdout", pack_id="tabular_classification_v1", phase="confirmatory",
            purpose="Assess the fixed two-feature contrast on published data; this is a mechanical pilot.",
            capture_source="capture", parameters={}, host_inputs={},
        )],
        literature=[
            dict(kind="open_source", url=SOURCE_URL),
            dict(kind="crossref_search", query="Banknote Authentication Lohweg wavelet classifier", rows=5),
        ],
        model=model, reasoning_effort=reasoning_effort,
    )
    if grouping is not None:
        train = grouping["split"]["train.csv"]["groups"]
        holdout = grouping["split"]["holdout.csv"]["groups"]
        config["budget"]["model_calls"] = 20
        config["scope"].update(
            population="Fixed rows from one historical public dataset, one row per exact projected feature pair.",
            grouping=(f"First source row per projected pair, selected without using labels before the fixed split. "
                      f"{train} train groups, {holdout} holdout groups, {grouping['dropped_rows']} rows dropped, "
                      f"{grouping['group_overlap']} groups cross splits."),
            evidence=("Exact duplicates removed. Dependence beyond equal pairs remains unmeasured. "
                      "Report the fixed-row accuracy difference without population or new-sample inference."),
        )
        config["candidates"][0].update(
            label="banknote-grouped-holdout",
            purpose="Assess the fixed contrast after selecting one record per exact projected pair.",
        )
    return config


def _grouped_reply(grouping: dict) -> str:
    train = grouping["split"]["train.csv"]["groups"]
    holdout = grouping["split"]["holdout.csv"]["groups"]
    dropped = grouping["dropped_rows"]
    overlap = grouping["group_overlap"]
    return (
        "Первый пилот остановлен до эксперимента, когда тестировщик поднял вопрос "
        "о повторяющихся наблюдениях. В пересмотренном захвате до разбивки оставлена первая "
        "строка исходного файла на точную пару преобразованных variance/skewness. Выбор "
        "представителя и назначение split не зависят от метки. "
        f"Проверка манифеста дала {train} групп train, {holdout} групп holdout, "
        f"{dropped} удалённых повторных строк и {overlap} общих пар. "
        "Оцениваем разницу точности только на этих строках. Схожие наблюдения или "
        "другие источники зависимости остаются возможными. Интервал и тест Мак-Немара "
        "в адаптере считаются по его механическому правилу. Они не обосновывают вывод "
        "о популяции на этих данных. Опубликованные данные и пересчёт в том же пакете "
        "не дают новой выборки. Роль рецензента даёт замечание по фактам этой сессии. "
        "Независимое научное заключение и проверка новизны остаются открытыми.\n"
    )


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def prepare_pilot(destination: Path, raw: bytes, *, model: str, reasoning_effort: str,
                  capture_version: int = 1) -> None:
    if capture_version not in (1, 2):
        raise ValueError("invalid pilot capture version")
    files, provenance = prepare_capture(raw, SOURCE_SHA256,
                                        collapse_projected_pairs=capture_version == 2)
    grouping = provenance.get("grouping")
    config = pilot_config(model, reasoning_effort, grouping=grouping)
    provenance.update(archive_url=ARCHIVE_URL, archive_sha256=ARCHIVE_SHA256)
    destination.mkdir()
    capture = destination / "capture"
    capture.mkdir()
    for name, data in files.items():
        (capture / name).write_bytes(data)
    (destination / SOURCE_NAME).write_bytes(raw)
    hypothesis = HYPOTHESIS if grouping is None else GROUPED_HYPOTHESIS
    reply = AUTHOR_REPLY if grouping is None else _grouped_reply(grouping)
    (destination / "hypothesis.txt").write_text(hypothesis, encoding="utf-8", newline="\n")
    (destination / "author-reply-template.txt").write_text(reply, encoding="utf-8", newline="\n")
    _write_json(destination / "config.json", config)
    _write_json(destination / "provenance.json", provenance)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new directory for the study inputs")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="local copy of the exact pinned UCI text file")
    source.add_argument("--fetch-uci", action="store_true", help="download the exact pinned UCI archive")
    parser.add_argument("--model", required=True, help="explicit Codex model slug")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--capture-version", type=int, choices=(1, 2), default=1,
                        help="v2 selects the first row per projected pair before splitting")
    args = parser.parse_args()
    try:
        if args.source is not None:
            with args.source.open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
        else:
            raw = _download()
        prepare_pilot(args.destination, raw, model=args.model, reasoning_effort=args.reasoning_effort,
                      capture_version=args.capture_version)
    except (OSError, ValueError) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        parser.exit(2, f"Banknote pilot preparation failed: {exc}\n")
    print(str(args.destination.resolve()))


if __name__ == "__main__":
    main()
