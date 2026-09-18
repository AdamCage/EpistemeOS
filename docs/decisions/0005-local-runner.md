# ADR 0005: однократный local dispatch и восстановление результата

Статус: первый реализованный срез M2, 18 сентября 2026; результаты инженерных проверок — в [validation](../validation.md). Это не приёмка полного M2. Основания: [архитектура](../architecture.md), [MVP-план](../mvp-plan.md), [command admission](0001-command-admission.md). Документ описывает выбранное ограничение исполнения: один dispatch на attempt, без leases, reclaim, автоматических retries и глобального resource ledger.

## Решение и граница доверия

Добавляется Execution service и общий локальный Python subprocess backend. Controller владеет Store; worker получает frozen job specification и отдельный рабочий каталог, выполняет процесс и записывает completion record. Worker не получает объект Store и не пишет scientific events. Controller проверяет запись, импортирует bytes в CAS и сохраняет terminal result.

Это доверенное локальное исполнение. Actor IDs, разные processes, Python `-I` и отдельный cwd не доказывают независимости участников и не запрещают программе читать файлы, сеть, environment или запускать дочерние процессы. В первом срезе нет sandbox на Windows; доступный и проверенный Docker/container backend не является предпосылкой. Отсутствующая capability не объявляется выполненной: запрос, требующий гарантированной изоляции или запрещённой сети, должен отклоняться до dispatch.

Job не выполняет научный review, не создаёт claim автоматически и не превращает exit 0 в научное подтверждение. Generic runner проверяет исполнение и формат outputs; пересчёт предметной метрики из raw data и соответствие analysis plan остаются задачей domain adapter и дальнейшего M2.

Python payload ждёт permit по stdin, пока supervisor назначает Windows Job Object либо создаёт POSIX process group. После выхода/timeout supervisor завершает оставшихся участников и проверяет окончание группы; при невозможности подтвердить это пишет unknown. На Linux worker становится subreaper и собирает завершившихся descendants. POSIX-профиль требует, чтобы доверенная программа не покидала process group. Основания реализации: [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject), [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject), [Linux subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html), [Python subprocess](https://docs.python.org/3/library/subprocess.html).

## Immutable job и admission

Одна короткая command transaction создаёт новый Kernel `run` и следующий за ним `execution_job`. Job содержит run ID/hash, mode, capabilities и digest frozen specification. Вместе эти записи связывают protocol ID/hash, seed, implementation/environment/input digests, argv, expected outputs и ограничения. Backend фиксирует фактические параметры runtime отдельно от декларации среды. Наличие environment blob само по себе не доказывает восстановление этой среды.

Очередь восстанавливается из events: job без dispatch имеет состояние `queued`. Отдельная изменяемая queue table не нужна для этого среза. CAS preparation допускается до transaction; сбой может оставить orphan bytes, но не partially committed run/job. Долгое выполнение и запуск процесса внутри `Store.command` запрещены.

Единственный реализуемый резерв — уже существующий `protocol.run_limit`: каждый зарегистрированный run занимает один attempt slot до запуска. Считаются queued, unknown, failed и completed attempts; terminal result не возвращает использованный slot. Это ограничение числа attempts, а не резерв времени, денег, памяти или GPU. Две enqueue-команды не могут одновременно получить последний slot благодаря admission под write transaction и повторной проверке состояния.

Все events одной command должны иметь её actor/role. Run, job и последующая финализация относятся к назначенному executor либо replicator. Caller-declared assignment сохраняет текущую доверенную модель; аутентифицированный worker assignment относится к дальнейшим этапам.

## Однократный dispatch

Dispatch command проверяет, что job существует, не имеет dispatch/finalization и связан с целым specification, затем сохраняет `execution_dispatch`. Эта запись является durable intent, а не доказательством успешного `Popen`. Только после commit controller передаёт job worker.

У одного job допускается ровно один dispatch. Конкурирующий controller или повторный вызов после restart не получает второе разрешение запуска. Leases, heartbeat ownership, автоматического reclaim и повторной отправки нет. После durable intent даже доказанно неисполненный из-за сбоя controller job не запускается заново в рамках того же attempt. Новая осознанная попытка требует нового run/job и расходует следующий slot; автоматический retry в этот срез не входит.

Особое правило receipt replay: возврат прежнего dispatch ID из `Store.command` **не является новым разрешением `Popen`**. `work_job` при существующем dispatch вызывает только reconciliation. Для нового dispatch каждый invocation создаёт новый command ID; конкурирующий или устаревший admission отвергается до `Popen`. Только успешно committed новое намерение в этом invocation приводит к запуску. Публичный command handler процесса не запускает, включая replay.

## Completion и reconciliation

Worker пишет completion в отдельный job directory, публикуя целую запись после подготовки outputs. Запись связывает job ID, run ID, dispatch identity и specification digest. Она содержит наблюдённые exit/timing, logs, runtime provenance, output names, sizes и hashes, а также точные ограничения backend. Произвольный оставшийся `completion.json` или устаревшие outputs не считаются результатом нового dispatch.

Controller принимает только ожидаемые относительные пути внутри job directory; абсолютные пути, traversal и ссылки наружу не становятся artifact sources. Он проверяет schema, identities, sizes/hashes и обязательные outputs. Данные импортируются в CAS, затем повторно проверяются на границе persisted transition. Незавершённая или повреждённая completion не допускает result. Partial logs сохраняются, когда доступны; отсутствие результата не подменяется нулевой метрикой.

`reconcile` выполняет одной command transaction Kernel terminal result и `execution_finalized`, ссылающийся на result и frozen completion/provenance digest. Проверяются отсутствие прежнего terminal result, принадлежность job/dispatch/run, назначенный actor/role и совпадение output mapping. Повтор принятой финализации возвращает прежний результат; новый вариант completion не переписывает его.

`completed` требует подтверждённого нормального завершения с exit 0 и пригодных обязательных outputs. Наблюдённый ненулевой exit может дать `failed` с logs и причиной. Timeout наблюдателя, исчезновение controller или отсутствие ответа не доказывают завершения процесса. Для `cancelled` требуется подтверждение выполненной отмены в заявленной области; если прекращение дочерних процессов не гарантировано, эта граница явно фиксируется и освобождение внешних ресурсов не заявляется.

## Неизвестный исход и crash windows

`unknown` — состояние dispatch, для которого нет проверенного completion/finalization; оно может включать всё ещё работающий процесс. Оно не записывается как выдуманный `failed` result. Существующий Kernel поддерживает terminal statuses `completed/failed/cancelled`; неизвестный run остаётся без result, сохраняет слот и не становится evidence для claim.

| Момент сбоя | Сохранённое состояние и действие |
| --- | --- |
| До commit enqueue | Нет job/run; возможны безвредные CAS orphans. Повтор исходной команды допустим. |
| После enqueue, до dispatch | `queued`; впервые dispatch можно выполнить после restart. |
| После commit dispatch, до `Popen` | Исход `unknown`; повтор того же attempt запрещён. Возможна намеренная потеря доступности ради отсутствия скрытого дубля. |
| После spawn, до сохранения PID или ответа controller | `unknown`; отсутствие PID не доказывает, что процесс не запускался. |
| Во время выполнения или записи completion | Сохраняется unknown до проверки целой completion. Нельзя освобождать ресурс на основании пропавшего observer. |
| Completion опубликована, result ещё не committed | `reconcile` проверяет прежний dispatch и завершает прежний run без нового исполнения. |
| Result/finalized committed, ответ потерян | Receipt replay либо чтение finalized state возвращает исходный result. Повторного worker нет. |

PID сам по себе не удостоверяет процесс: он может быть переиспользован. Если последующее расширение будет проверять или завершать процессы, нужны process creation identity и проверенная схема владения. Этот срез не обещает безопасный reclaim по одному PID.

## Binding к run и evidence basis

Менять опубликованный payload `run` и v1 signatures/defaults Kernel не требуется. Достаточно отдельного `execution_job(run, specification_digest)`, если соблюдены следующие инварианты:

- Job создаётся только вместе с новым run, непосредственно после него в одной enqueue transaction/receipt. Прикрепление managed job к уже существовавшему legacy run запрещено.
- Job уникален для run. Его actor/role и specification совпадают с frozen run/protocol; mismatched protocol hash, seed, argv, implementation или environment отвергаются.
- Dispatch уникален для job и следует после него. Finalized ссылается на этот dispatch и единственный terminal result; hashes и identity проверяются на соответствующем историческом prefix.

Это не позволяет задним числом объявить старый caller-recorded run исполненным backend. Receipt удостоверяет atomic admission в принятой локальной модели, а scientific basis получает сами immutable events и artifact references. Рецепт review basis для histories без managed jobs остаётся прежним.

Точное расширение `_local_evidence`: сохранить существующий список plan/claim/runs/results/exposure/planning context, затем добавить связанные `execution_job`, `execution_dispatch`, `execution_finalized` всех runs данного protocol в порядке `seq`, без дубликатов. Учитываются и нецитируемые failed/unknown attempts. Нерелевантные jobs и изменяемое содержимое рабочего каталога в basis не входят. Полные event payloads связывают specification и completion digests; gates дополнительно проверяют bytes всех referenced CAS artifacts.

Следствия: новый dispatch/finalization меняет evidence basis и требует актуального review; последующее чтение job directory ничего не переписывает. Historical Graph/review используют только execution events до соответствующей decision. Удалённый либо изменённый source, manifest, log или output блокирует gate, даже если run failed. При полностью legacy history не добавляется пустой execution wrapper, который изменил бы прежние basis hashes.

## Защита terminal transition

Публичный `Kernel.finish_run` обязан отклонять managed run: иначе тот же caller мог бы создать job и сразу записать `completed` с произвольными blobs, обойдя worker completion. Опубликованная сигнатура метода не меняется; запрет определяется наличием execution_job в проверенной history.

Execution reconciliation использует внутренний validated terminal path и записывает result вместе с finalized marker в одной transaction. Это предотвращает обычный API bypass, но не превращает private Python method в security boundary против владельца процесса или файлов. Graph и mechanical gates независимо отвергают managed completed/failed/cancelled result без согласованного finalized record и provenance closure. Одна лишь проверка в CLI недостаточна.

Новые event kinds и artifact references требуют поддержки Graph, reporting и recovery verification. Bundle/manuscript раскрывают execution mode, actual provenance и isolation limitations; managed execution не должно выглядеть как независимый scientific review.

## Search, backup и оставшийся M2

Search selection уже имеет отдельный резерв объявленной стоимости; это не runner resource ledger. `Execution.enqueue` пока не принимает selection ID. Существующий `Search.finish_selection` связывает выбранный protocol с фактическим terminal run отдельной planner-командой; unknown run не удовлетворяет этому условию. Planner `search_terminal` нельзя смешивать в одной command с executor result из-за actor/role invariant. Автоматический dispatch из tree и сверка фактических ресурсов остаются открытыми.

Текущий backup покрывает SQLite и CAS. Рабочий job directory и ещё не импортированная completion в него автоматически не входят. После restore dispatched job без completion остаётся unknown; для его reconciliation может потребоваться исходный job directory. Finalized completion/provenance должны быть CAS artifacts и тогда входят в snapshot.

Гарантия однократного dispatch относится к одной истории Store. Восстановление старого queued snapshot не доказывает, что исходный store не выполнил job позднее. Writable clones, параллельно работающие с одной внешней системой, не получают exactly-once гарантию от SQLite backup. В этом срезе нет автоматического возобновления восстановленных очередей или переноса владения живыми процессами; такой сценарий требует отдельного решения об ownership/reconciliation.

Остаются открытыми: leases и heartbeat ownership/reclaim, автоматические retry policies, study/experiment resource ledger, реальные money/token/GPU costs, аутентифицированные assignments, изоляция от процессов вне контролируемой POSIX group, проверенная OS/network/filesystem изоляция, полное environment reconstruction, domain metric recomputation, отдельная attestation boundary и внешний checkpoint. Диагностический heartbeat file worker не является lease или основанием для повторного запуска.

## Приёмочные проверки первого среза

Нужны проверки atomic enqueue и последнего protocol slot; двух конкурирующих dispatch; replay dispatch без повторного процесса; сбоев между intent/spawn/completion/finalize; отсутствия автоматического retry при unknown; поздней корректной completion и идемпотентной reconciliation; отказа при mismatched identity, corrupted/missing output или unsafe path; direct finish bypass; изменения review basis после execution events; исторической Graph-проекции и backup/restore finalized provenance. Реальный небольшой offline Python fixture проверяет путь исполнения без научного approval.

Этот перечень задаёт критерии, а не утверждает результат ещё не завершённого test run. Итоговые команды, платформенные ограничения и число прошедших тестов фиксируются в validation report после реализации.
