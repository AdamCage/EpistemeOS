"""Read-only publication of a locally scoped research dossier and exact artifact bytes."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .graph import NodeKind, ResearchGraph
from .kernel import Actor, Kernel, require
from .package import package_files
from .research import session_view
from .research_controller import research_status
from .reporting import paper_status
from .store import Store, canonical, digest


def _line(value: str) -> str:
    text = " ".join(value.split()).replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _manuscript(status: dict[str, Any], rows: list[dict[str, Any]]) -> bytes:
    disposition = status["disposition"]
    sections = ["# Внутренняя исследовательская рукопись", "", "## Гипотеза", "", _line(status["hypothesis"]), ""]
    if status["replies"]:
        sections.extend(["## Условия автора", "", *[f"- {_line(reply)}" for reply in status["replies"]], ""])
    sections.extend(["## Что установлено", "", f"- Состояние цикла `{disposition['reason']}`.",
                     f"- {_line(disposition['detail'])}",
                     "- Scientific validity `not_assessed`."])
    if status["fixture_provider"]:
        sections.append("- В работе участвовали детерминированные тестовые роли.")
    sections.extend(["", "## Методы и результаты", ""])
    if not status["stage_results"]:
        sections.append("Исполненный эксперимент с допущенным анализом отсутствует в этом досье.")
    for stage in status["stage_results"]:
        sections.extend([f"- Stage `{stage['stage']}`. Протокол `{stage['protocol']}`. "
                         f"Механический статус `{stage['disposition']}`. "
                         f"Анализ `{stage['analysis']}`. Вывод `{stage['outcome']}`. "
                         f"Режим вывода `{stage['inference_mode']}`. Закрытие `{stage['close']}`."])
        if stage["claim"]:
            sections.append(f"  [Материалы утверждения {stage['claim']}](claims/{stage['claim']}/results.md)")
    sections.extend(["", "## Предшествующие работы", "",
                     "Поиск ограничен зафиксированными источниками. Новизна за пределами этого охвата остаётся открытой."])
    for source in status["literature"]:
        sections.append(f"- `{source['id']}` · `{source.get('content_kind', 'unavailable')}` · "
                        f"`{source.get('artifact_digest') or _line(source['failure'])}`")
    sections.extend(["", "## Критические замечания и границы вывода", ""])
    for row in rows:
        p = row["payload"]
        if p["step"] != "task" or not p["key"].startswith(("task:reviewer:", "task:critic")):
            continue
        data = p["data"]["result"]
        answer = data.get("answer") or {}
        reason = answer.get("reason") or data.get("reason")
        if reason:
            sections.append(f"- `{row['id']}` · `{answer.get('decision', data['status'])}` · {_line(reason)}")
    sections.extend(["", "Исполнители пакетов используют заявленный режим повторного расчёта. "
                     "Проверенная независимость рецензента и репликация на новых данных в этом контуре не установлены.", ""])
    return "\n".join(sections).encode()


def export_research(store: Store, session: str, destination: Path) -> dict[str, Any]:
    destination = destination.absolute()
    require(not destination.exists() and not destination.is_symlink(), "research export destination already exists")
    require(destination.parent.is_dir(), "research export parent directory must exist")
    target = destination.resolve()
    require(not target.is_relative_to(store.root) and not store.root.is_relative_to(target),
            "research export must be outside the store")
    with store.reading():
        status = research_status(store, session)
        require(status["status"] == "closed", "research must close before export")
        history, receipts = store.snapshot()
        graph = ResearchGraph.from_store(store)
        view = session_view(history, session)
        ids = {session, *(row["id"] for row in view["rows"])}
        ids.update(id for receipt in receipts if receipt["context"]["correlation_id"] == session
                   for id in receipt["event_ids"])
        lineage = {node.id: node for id in list(ids) for node in graph.ancestors(id)}
        artifacts = {node.payload["sha256"] for node in lineage.values() if node.kind == NodeKind.ARTIFACT}
        ids.update(node.id for node in lineage.values() if node.kind != NodeKind.ARTIFACT)
        files = {"research.json": canonical(status) + b"\n", "manuscript.md": _manuscript(status, view["rows"]),
                 "events.jsonl": b"".join(canonical(row) + b"\n" for row in history if row["id"] in ids),
                 "receipts.jsonl": b"".join(canonical(row) + b"\n" for row in receipts if set(row["event_ids"]) & ids)}
        picked_paper = status["disposition"]["paper"]
        if picked_paper:
            paper = next(row for row in history if row["id"] == picked_paper)
            reader = Kernel(store, Actor("research-export", "observer"))
            decisions = {id: reader._next_action(history, id) for id in paper["payload"]["claims"]}
            if paper_status(store, history, paper, decisions) == "current":
                files["reviewed-manuscript.md"] = store.read(paper["payload"]["manuscript"])
        for row in status["disposition"]["outcomes"]:
            claim = row["claim"]
            files.update({f"claims/{claim}/{key}": value for key, value in package_files(store, claim).items()})
        # Keep large bytes out of the in-memory list of files.
        members = [dict(path=key, sha256=digest(value), bytes=len(value)) for key, value in sorted(files.items())]
        with TemporaryDirectory(dir=destination.parent, prefix=".research-export-") as temporary:
            complete = Path(temporary) / "bundle"
            complete.mkdir()
            for key, value in files.items():
                path = complete / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            (complete / "artifacts").mkdir()
            for key in sorted(artifacts):
                value = store.read(key)
                path = f"artifacts/{key}"
                (complete / path).write_bytes(value)
                members.append(dict(path=path, sha256=key, bytes=len(value)))
            (complete / "manifest.json").write_bytes(canonical(dict(schema_version=1, session=session,
                graph_head=graph.snapshot_hash, files=sorted(members, key=lambda item: item["path"]))) + b"\n")
            require(not destination.exists() and not destination.is_symlink(), "research export destination appeared")
            complete.rename(destination)
    return dict(session=session, directory=str(destination), files=len(members) + 1,
                scientific_validity="not_assessed")
