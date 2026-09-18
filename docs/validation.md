# Проверка текущего прототипа

Дата: 18 сентября 2026. Среда: Windows, Python 3.11.15, локальный virtualenv через uv. Это инженерная проверка первого этапа, инкрементов M1 и первого local runner M2, не оценка качества научных открытий.

## Выполненные проверки

`uv run python -m unittest discover -s tests -v`: **289 тестов, 286 успешно, 3 skipped**, 139.944 секунды. Локальный transcript: `.research/runner-tests-20260918.log` (git-ignored). Пропущены три теста создания symlink: среда Windows не предоставляет это право. Отказ от symlink entries реализован, но эти cases на данном хосте не проверены. Предыдущие инкременты: 9 сентября — 186 тестов, 185 успешно, 1 skipped; 11 сентября — 226 тестов, 225 успешно, 1 skipped; 16 сентября — 239 тестов, 237 успешно, 2 skipped; 17 сентября — 268 тестов, 266 успешно, 2 skipped.

Первый commit `46c783b` опубликован в публичном [GitHub repository](https://github.com/AdamCage/EpistemeOS) и прошёл [CI run 34277599425](https://github.com/AdamCage/EpistemeOS/actions/runs/34277599425). M1 increment [`91192d4`](https://github.com/AdamCage/EpistemeOS/commit/91192d4) отдельно прошёл [CI run 34385764051](https://github.com/AdamCage/EpistemeOS/actions/runs/34385764051): все четыре jobs успешны — Windows/Linux, Python 3.11/3.13.

Инкремент claim relations [`a1bcc6c`](https://github.com/AdamCage/EpistemeOS/commit/a1bcc6c) опубликован 16 сентября и прошёл [CI run 35113659014](https://github.com/AdamCage/EpistemeOS/actions/runs/35113659014): все четыре jobs Windows/Linux, Python 3.11/3.13 успешны.

В [первом CI backup/restore](https://github.com/AdamCage/EpistemeOS/actions/runs/35114984511) обе Linux jobs прошли, Windows 3.13 выявил ошибочное ожидание теста: `readlink()` возвращал verbatim path с префиксом, отсутствующим в исходном `Path`. 17 сентября тест исправлен: сравнивает фактическое содержимое ссылки до и после отказа операции. Локально focused recovery suite прошла 8 проверок и пропустила symlink case; для исполнения этого case нужны права CI runner.

Исправление [`8183e47`](https://github.com/AdamCage/EpistemeOS/commit/8183e47) прошло [CI run 35197219085](https://github.com/AdamCage/EpistemeOS/actions/runs/35197219085): все четыре jobs Windows/Linux и Python 3.11/3.13 успешны.

Planning increment [`1301083`](https://github.com/AdamCage/EpistemeOS/commit/1301083) прошёл [CI run 35232388814](https://github.com/AdamCage/EpistemeOS/actions/runs/35232388814) во всех четырёх конфигурациях. Платформенная проверка нового runner публикуется отдельным CI после commit; локальный результат выше относится к Windows 3.11.

`uv run python -m compileall -q src`, `uv lock --check`, `uv sync` — выполнены успешно. У runtime нет сторонних зависимостей; uv.lock не фиксирует весь Python/OS или build toolchain.

Шесть публичных JSON schemas и содержащиеся в них examples проверены `jsonschema.Draft202012Validator` в отдельном установленном окружении. `jsonschema` не добавлен в runtime. Schema validation проверяет форму; исторические ссылки, bases и условия переходов проверяются Kernel и workflow tests.

| Проверяемая область | Сценарии |
|---|---|
| Kernel | Preregistration до run, frozen source/environment, scope, полнота seeds, отрицательные результаты и failed logs, self-review включая автора гипотезы, stale basis, review veto. |
| Local execution | 21 новый test: реальные CPU jobs и CLI, atomic attempt reservation/последний slot, concurrent controllers, dispatch/finalize replay, controller kill до completion, reconcile без spawn, unknown после intent и restore, Graph/CAS closure, отказ direct finish/forged result, raw data reanalysis, nonzero exit, invalid metric, missing/oversized outputs, input/runtime drift, bounded stdout, живые descendants при timeout и после выхода parent. Один symlink-output case локально skipped. |
| Store | Corruption, missing artifacts, append-only triggers, conflicting writers, reopen, read-only inspection. |
| Recovery | 13 новых тестов: exact events/receipts/graph round-trip, stale command replay и altered-body conflict, orphan CAS, byte corruption, invalid receipt/graph, manifest paths/duplicate keys/unknown version, active transaction refusal, existing target/containment, real CLI. Writer после SQLite snapshot не смешивает revisions; два restore не заменяют общий destination; пересчитанный DB checksum не скрывает нарушенный receipt binding. |
| Planning lineage | 29 новых тестов: strict Q/set payloads, hashes/scope, current heads, exact exclusion reasons, concurrent revision conflict, исторический context без descendants. Protocol derives pool/scope, сохраняет binding и не сбрасывает lineage в amendment. Replay переживает новые heads; study mismatch блокирует bound run и tree selection без events/receipts. Reviewer conflicts включают ancestors и удалённые hypotheses; Graph проверяет forged binding; CLI и recovery сохраняют версии. Paper раскрывает исключённую альтернативу и причину её исключения. |
| Command delivery | Два concurrent writers с одинаковым ID/body и с разными IDs; два события + receipt атомарно; lost response после `os._exit`; rollback-only после подавленной ошибки append; receipt corruption/range/overlap; readonly legacy schema и additive migration с неизменным JSONL. |
| Versioned service | Role/type/argument admission до handler; omitted/explicit defaults; replay terminal run, search reservation, review/paper после смены evidence; strict JSON; real CLI reopen и receipts. |
| Statistical workflow | Null-result остаётся inconclusive/not_assessed; metric/stopping/unit mismatch; protected split до просмотра; started/failed inputs без parent не становятся новым holdout; typed amendments сохраняют reason/seen_data; foreign attempt и declared exposure требуют fresh review; unrelated inherited snapshot не инвалидирует basis. |
| Claim relations | 40 новых тестов: typed links, exact scope, направление context, cycles/duplicates, stale/concurrent admission, transitive basis и artifacts, contributor/veto guards, explicit assessments, историческая/текущая readiness, supersession, historical graph bindings, paper context, command replay и реальный CLI. Многоступенчатая lineage A → B → C проверяет current readiness последней принятой версии и historical readiness внутренних звеньев. Open foreign findings требуют acknowledgement; оно не закрывает исходное veto; симметричные closures сходятся без взаимного бесконечного сброса approvals. |
| Search | A/B ballots, ties/abstentions, priority≠truth, persistent frontier, актуальность протокола перед selection, общий repair-lineage limit, reservations, overrun, точная арифметика больших и subnormal decimal costs. |
| Reporting | Один snapshot при concurrent append, generic primary metric, запрет premature paper, отрицательное review, concurrent evidence mutation перед commit, CAS manuscript. |
| Graph | Типы/порядок/closure ссылок, ancestors/descendants, exact scope, исторические revisions, corruption, afterlife snapshot без accepted claims. |
| Import | Идемпотентность, неизменность исходника, ограничение чтения/копирования, missing/corrupt/unsafe outputs, dirty/failed/superseded states, отсутствие ретроактивной preregistration. |
| CLI/workflow | Реальные CPU subprocesses, повторное вычисление по raw CSV, durable failure, reopen/export, search selection до run и сохранение невыбранной альтернативы. |

## Сохранённый пример command delivery

Новые `kernel.link_claims` и `kernel.review_with_links` дополнительно проверены через отдельные CLI subprocesses в `test_cli_link_and_unresolved_review_persist_replay_and_export_graph`: два повторно доставленных requests создают ровно одну связь и одно review с `request_changes`, две receipts; последующий CLI graph читает эти события. Все outputs и мнения этого теста явно synthetic fixtures; научный эксперимент и научное approval не выполняются.

Один example из публичной command schema дважды отправлен реальным CLI в `.research/command-example-20260909`. Оба процесса вернули `hypothesis-570a0cac8e85485e`; после read-only reopen в Store ровно одно событие и одна receipt. Event hash: `dfe8e18f8adc106904662a7278b14916846f7ee396b34372088c74dcf64c0ad5`. [Локальная проверка](../.research/command-example-20260909/verification.json) и request находятся в ignored каталоге. Это доставка fixture hypothesis, без эксперимента или scientific approval.

## Сохранённый demo

18 сентября выполнен `uv run python examples/local_execution.py --root .research/local-runner-verified-20260918`: primary и отдельный повторный анализ через общий backend, шесть command receipts, 14 events, 27 Graph nodes/61 edges. Head: `3fb1ece148e9121c31c931119eace707a8d1467aa5fdd2b0fa1c6508d440afec`; claim `claim-976be9541b184cf7`; basis `f48941396cc6f3cbfb254f021fba042ad098d6bfa71a70df21f023560862cf42`. Вычисленный mean равен 2 на трёх synthetic values. Gate прошёл, `scientific_validity=not_assessed`, next action `scientific_review`; review/approval не создавались. [Локальный report](../.research/local-runner-verified-20260918/report.md), bundle и outputs git-ignored. Environment содержит native OS/interpreter fingerprint, но не portable environment closure; Linux process control требует отдельного CI.

16 сентября CLI `backup`/`restore` проверены на `.research/command-example-20260909` и `.research/search-demo-20260908`, с восстановлением в новые каталоги `command-restored-20260916` и `search-restored-20260916`. Сравнены полные event JSONL, receipt JSONL и Graph: все совпали. Повтор исходного command envelope в восстановленном Store вернул тот же `hypothesis-570a0cac8e85485e`, сохранив одно событие и одну receipt. Demo сохранил 26 событий, 45 nodes/112 edges и прежний head hash. [Локальная проверка восстановления](../.research/recovery-verification-20260916.json) и directory snapshots остаются git-ignored; это перенос существующего evidence, без нового научного эксперимента или approval.

Команда: `uv run episteme demo --with-search --root .research/search-demo-20260908`.

Снимок: `f445634f0c73191d9611ca09a643cc20bc8e2f7a74fd7366cf532a16a918a9a3`; 26 событий. Claim: `claim-375e8b03ab02499b`; basis: `3bd3ef7dfc29cfb687897d7f0d4f1facd2fa2267bfb143c7f1a5c56a66655c1a`.

Два варианта experiment spec, один выполненный, второй сохранён в frontier после исчерпания бюджета. Выполнены 3 primary и 3 reanalysis subprocesses. Gate прошёл, `scientific_validity=not_assessed`, следующий шаг — `scientific_review`. Reviewer approval и paper в demo отсутствуют. Баллы, роли и модель данных synthetic fixture заданы кодом; это не проверка LLM-планировщика или clean-room независимости.

`episteme graph` проверил 45 nodes и 112 edges. [Локальный отчёт](../.research/search-demo-20260908/report.md) и [bundle](../.research/search-demo-20260908/review-bundle.json) находятся в git-ignored research directory; для переноса нужно выполнить demo заново либо передать весь каталог со всеми blobs. Тест workflow дополнительно выполняет актуальную версию кода в новом временном каталоге.

## Исторический afterlife

Импортирован read-only checkout `C:/Projects/llm-semantic-afterlife`, SHA `4656b2ceda7bd86b6c213bc426d07e490d178405`, source tree clean до/после. Команда: `uv run episteme afterlife import C:\Projects\llm-semantic-afterlife --root .research/afterlife-history-20260908 --max-verify-mib 64`.

Получены 52 run records: 43 COMPLETED и 9 FAILED; 38 records с dirty source, 1 superseded. Сохранены metadata 50 artifact bundles и 17 документов; 166 blobs, 2 392 933 bytes. Проверено 67 107 499 bytes outputs: 285 совпадений hash, 259 пропусков по общему бюджету и 1 по размеру отдельного файла. Эти skips не превращены в подтверждение.

Snapshot ID: `66ebe1b365171db89c5b88b2d8d2735aee00f43c397d12a32539e5d2538dea59`. Повторный импорт вернул `created=false`, сохранив одно событие. Graph: 168 nodes, 167 edges — исторический snapshot и его blob closure. Created preregistered protocols: 0; accepted claims: 0. [Локальный bundle](../.research/afterlife-history-20260908/review-bundle.json) остаётся `historical_unverified`.

## Незавершённое

Общая цель ещё не достигнута: реальных provider agents, аутентифицированных назначений и контекстной/OS изоляции, полного runner с environment reconstruction/resource ledger/leases, автоматического review-driven replanning и полного manuscript pipeline пока нет. Первый trusted local runner восстанавливает результат по completion без повторного исполнения; это часть M2. M1–M6 остаются в [плане](mvp-plan.md); прототип закрывает только указанные сценарии.

Docker CLI обнаружен, но проверка `docker version` не смогла подключиться к Linux engine named pipe; повторная read-only проверка 17 сентября дала тот же результат. Sandbox на этом хосте не проверялся. Новый Windows backend использует Job Object для жизненного цикла процессов, сохраняя обычные права пользователя на файлы/сеть.

Независимый технический [review от 7 сентября](implementation-review.md) сохраняет результаты того запуска. Отмеченное там отсутствие защиты от review автором гипотезы исправлено и проверено отдельным `test_hypothesis_author_cannot_review_when_another_actor_registered_protocol`; исторический отчёт не переписан как будто исправление было проверено раньше.
