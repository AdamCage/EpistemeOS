# Проверка текущего прототипа

Дата: 9 сентября 2026. Среда: Windows, Python 3.11.15, локальный virtualenv через uv. Это инженерная проверка первого этапа и инкремента M1, не оценка качества научных открытий.

## Выполненные проверки

`uv run python -m unittest discover -s tests -v`: **186 тестов, 185 успешно, 1 skipped**, 30.543 секунды. Пропущен тест создания symlink: среда Windows не предоставляет это право. Обход reparse points реализован, но настоящий symlink case на этом хосте не проверен.

Первый commit `46c783b` опубликован в публичном [GitHub repository](https://github.com/AdamCage/EpistemeOS) и прошёл [CI run 34277599425](https://github.com/AdamCage/EpistemeOS/actions/runs/34277599425). M1 increment [`91192d4`](https://github.com/AdamCage/EpistemeOS/commit/91192d4) отдельно прошёл [CI run 34385764051](https://github.com/AdamCage/EpistemeOS/actions/runs/34385764051): все четыре jobs успешны — Windows/Linux, Python 3.11/3.13.

`uv run python -m compileall -q src`, `uv lock --check`, `uv sync` — выполнены успешно. У runtime нет сторонних зависимостей; uv.lock не фиксирует весь Python/OS или build toolchain.

| Проверяемая область | Сценарии |
|---|---|
| Kernel | Preregistration до run, frozen source/environment, scope, полнота seeds, отрицательные результаты и failed logs, self-review включая автора гипотезы, stale basis, review veto. |
| Store | Corruption, missing artifacts, append-only triggers, conflicting writers, reopen, read-only inspection. |
| Command delivery | Два concurrent writers с одинаковым ID/body и с разными IDs; два события + receipt атомарно; lost response после `os._exit`; rollback-only после подавленной ошибки append; receipt corruption/range/overlap; readonly legacy schema и additive migration с неизменным JSONL. |
| Versioned service | Role/type/argument admission до handler; omitted/explicit defaults; replay terminal run, search reservation, review/paper после смены evidence; strict JSON; real CLI reopen и receipts. |
| Statistical workflow | Null-result остаётся inconclusive/not_assessed; metric/stopping/unit mismatch; protected split до просмотра; started/failed inputs без parent не становятся новым holdout; typed amendments сохраняют reason/seen_data; foreign attempt и declared exposure требуют fresh review; unrelated inherited snapshot не инвалидирует basis. |
| Search | A/B ballots, ties/abstentions, priority≠truth, persistent frontier, актуальность протокола перед selection, общий repair-lineage limit, reservations, overrun, точная арифметика больших и subnormal decimal costs. |
| Reporting | Один snapshot при concurrent append, generic primary metric, запрет premature paper, отрицательное review, concurrent evidence mutation перед commit, CAS manuscript. |
| Graph | Типы/порядок/closure ссылок, ancestors/descendants, exact scope, исторические revisions, corruption, afterlife snapshot без accepted claims. |
| Import | Идемпотентность, неизменность исходника, ограничение чтения/копирования, missing/corrupt/unsafe outputs, dirty/failed/superseded states, отсутствие ретроактивной preregistration. |
| CLI/workflow | Реальные CPU subprocesses, повторное вычисление по raw CSV, durable failure, reopen/export, search selection до run и сохранение невыбранной альтернативы. |

## Сохранённый пример command delivery

Один example из публичной command schema дважды отправлен реальным CLI в `.research/command-example-20260909`. Оба процесса вернули `hypothesis-570a0cac8e85485e`; после read-only reopen в Store ровно одно событие и одна receipt. Event hash: `dfe8e18f8adc106904662a7278b14916846f7ee396b34372088c74dcf64c0ad5`. [Локальная проверка](../.research/command-example-20260909/verification.json) и request находятся в ignored каталоге. Это доставка fixture hypothesis, без эксперимента или scientific approval.

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
