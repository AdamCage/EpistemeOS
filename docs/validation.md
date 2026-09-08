# Проверка текущего прототипа

Дата: 8 сентября 2026. Среда: Windows, Python 3.11.15, локальный virtualenv через uv. Это инженерная проверка первого этапа, не оценка качества научных открытий.

## Выполненные проверки

`uv run python -m unittest discover -s tests -v`: **122 теста, 121 успешно, 1 skipped**, 36.837 секунды. Пропущен тест создания symlink: среда Windows не предоставляет это право. Обход reparse points реализован, но настоящий symlink case на этом хосте не проверен. Linux/другие версии Python добавлены в CI matrix; удалённый CI ещё не запускался.

`uv run python -m compileall -q src`, `uv lock --check`, `uv sync` — выполнены успешно. У runtime нет сторонних зависимостей; uv.lock не фиксирует весь Python/OS или build toolchain.

| Проверяемая область | Сценарии |
|---|---|
| Kernel | Preregistration до run, frozen source/environment, scope, полнота seeds, отрицательные результаты и failed logs, self-review включая автора гипотезы, stale basis, review veto. |
| Store | Corruption, missing artifacts, append-only triggers, conflicting writers, reopen, read-only inspection. |
| Search | A/B ballots, ties/abstentions, priority≠truth, persistent frontier, актуальность протокола перед selection, общий repair-lineage limit, reservations, overrun, точная арифметика больших и subnormal decimal costs. |
| Reporting | Один snapshot при concurrent append, generic primary metric, запрет premature paper, отрицательное review, concurrent evidence mutation перед commit, CAS manuscript. |
| Graph | Типы/порядок/closure ссылок, ancestors/descendants, exact scope, исторические revisions, corruption, afterlife snapshot без accepted claims. |
| Import | Идемпотентность, неизменность исходника, ограничение чтения/копирования, missing/corrupt/unsafe outputs, dirty/failed/superseded states, отсутствие ретроактивной preregistration. |
| CLI/workflow | Реальные CPU subprocesses, повторное вычисление по raw CSV, durable failure, reopen/export, search selection до run и сохранение невыбранной альтернативы. |

## Сохранённый demo

Команда: `uv run episteme demo --with-search --root .research/search-demo-20260908`.

Снимок: `f445634f0c73191d9611ca09a643cc20bc8e2f7a74fd7366cf532a16a918a9a3`; 26 событий. Claim: `claim-375e8b03ab02499b`; basis: `3bd3ef7dfc29cfb687897d7f0d4f1facd2fa2267bfb143c7f1a5c56a66655c1a`.

Два варианта experiment spec, один выполненный, второй сохранён в frontier после исчерпания бюджета. Выполнены 3 primary и 3 reanalysis subprocesses. Gate прошёл, `scientific_validity=not_assessed`, следующий шаг — `scientific_review`. Reviewer approval и paper в demo отсутствуют. Баллы, роли и модель данных synthetic fixture заданы кодом; это не проверка LLM-планировщика или clean-room независимости.

`episteme graph` проверил 45 nodes и 112 edges. [Локальный отчёт](../.research/search-demo-20260908/report.md) и [bundle](../.research/search-demo-20260908/review-bundle.json) находятся в git-ignored research directory; для переноса нужно выполнить demo заново либо передать весь каталог со всеми blobs. Тест workflow дополнительно выполняет актуальную версию кода в новом временном каталоге.

## Исторический afterlife

Импортирован read-only checkout `C:/Projects/llm-semantic-afterlife`, SHA `4656b2ceda7bd86b6c213bc426d07e490d178405`, source tree clean до/после. Команда: `uv run episteme afterlife import C:\Projects\llm-semantic-afterlife --root .research/afterlife-history-20260908 --max-verify-mib 64`.

Получены 52 run records: 43 COMPLETED и 9 FAILED; 38 records с dirty source, 1 superseded. Сохранены metadata 50 artifact bundles и 17 документов; 166 blobs, 2 392 933 bytes. Проверено 67 107 499 bytes outputs: 285 совпадений hash, 259 пропусков по общему бюджету и 1 по размеру отдельного файла. Эти skips не превращены в подтверждение.

Snapshot ID: `66ebe1b365171db89c5b88b2d8d2735aee00f43c397d12a32539e5d2538dea59`. Повторный импорт вернул `created=false`, сохранив одно событие. Graph: 168 nodes, 167 edges — исторический snapshot и его blob closure. Created preregistered protocols: 0; accepted claims: 0. [Локальный bundle](../.research/afterlife-history-20260908/review-bundle.json) остаётся `historical_unverified`.

## Незавершённое

Общая цель ещё не достигнута: реальных provider agents, аутентифицированных назначений и контекстной/OS изоляции, универсального исполнителя, durable job recovery, автоматического review-driven replanning и полного manuscript pipeline пока нет. M1–M6 остаются в [плане](mvp-plan.md); прототип закрывает только указанные сценарии.

Docker CLI обнаружен, но проверка `docker version` не смогла подключиться к Linux engine named pipe. Sandbox на этом хосте не проверялся. Это ограничение следующего execution-этапа; локальные фикстуры используют обычный Python subprocess.

Независимый технический [review от 7 сентября](implementation-review.md) сохраняет результаты того запуска. Отмеченное там отсутствие защиты от review автором гипотезы исправлено и проверено отдельным `test_hypothesis_author_cannot_review_when_another_actor_registered_protocol`; исторический отчёт не переписан как будто исправление было проверено раньше.
