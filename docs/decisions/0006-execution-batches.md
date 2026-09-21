# ADR 0006: выбранный эксперимент → полный набор исполнений

Дата: 21 сентября 2026. Статус: локальная реализация M2; качество научного исследования и автономные агенты не оценены.

## Задача

`Search.select_next` резервирует выбранный experiment node. Прежний `finish_selection` закрывает его по одному run, хотя protocol требует primary и повторный анализ для каждого seed. Runner сохраняет отдельные jobs, но сам не знает полноты исследовательского плана. Нужен сохраняемый набор попыток с проверкой зависимостей, стоимости и восстановления после прерывания.

## Контракт

Planner вызывает `batch.plan` для открытого selection. В immutable `batch_plan` фиксируются selection/node/tree/protocol IDs и hashes, executor/replicator IDs, source переанализа, отличающийся по hash, и отдельно зафиксированный environment, output paths, лимиты и capabilities. Roster содержит `primary:<seed>` и `reanalysis:<seed>` для каждого зарегистрированного seed. Порядок исполнения — primary, его переанализ, следующий seed. Все допустимые независимые slots исполняются, даже если прежний primary завершился подтверждённой ошибкой.

Первый policy поддерживает только свежие primary nodes `baseline`, `discriminate`, `ablate`, `robustness`. Protocol с прежними runs, уже закрытый selection, чужая активная reservation или повторный batch отклоняются. `retry`, `debug`, `replicate` требуют других roster policies и пока не поддерживаются этим слоем. Выбор Search и batch admission — два отдельных сохраняемых перехода; сбой между ними оставляет открытый резерв. Нет автоматического выбора следующего узла или переноса reviewer findings в новые protocols.

Действия проходят через CommandService:

| Action | Actor | Атомарная запись |
|---|---|---|
| `batch.plan` | planner | Frozen batch и полное расписание slots |
| `batch.enqueue_slot` | Назначенный executor либо replicator | `run → execution_job → batch_slot` в одной receipt |
| `batch.settle` | Автор batch с ролью planner | `batch_settlement → search_terminal` с полным набором slots/runs/results |

Один `(batch, slot)` получает не более одного run. Reanalysis создаётся только после verified completed primary и получает его точные raw-data bytes. Код переанализа заморожен заранее; его отличие по hash и actor ID проверяется, но независимость авторства/контекста этим не доказана. Labels задаёт доверенный caller. Controller использует эти сохранённые назначения; аутентификации и отдельных AI sessions пока нет.

До settlement protocol зарезервирован за batch: обычные `kernel.start_run` и `execution.enqueue` не могут занять будущие slots. Публичные signatures/defaults прежних v1-команд сохранены; private Python helpers не являются защитой от владельца процесса. Историческая projection дополнительно проверяет recipes, отсутствие посторонних runs, соседство событий, полные command receipts и terminal evidence до момента settlement.

## Стоимость и состояния

Используется только `cost_unit=enqueued_attempt`. Резерв равен `2 × число seeds`; node estimate обязан совпадать с ним. Итоговый расход — число созданных managed jobs, включая failed. Это не CPU/GPU time, токены, деньги или число независимых наблюдений. Нельзя подставлять произвольную единицу существующего дерева.

| Slot state | Смысл |
|---|---|
| `pending` / `waiting_primary` | Run ещё не создан; зависимость может быть незавершённой |
| `queued` | Run/job уже сохранены, dispatch отсутствует |
| `unknown` | Dispatch сохранён, verified terminal result отсутствует; процесс может ещё работать |
| `completed` / `failed` | Проверенные runner outputs и terminal evidence |
| `blocked_dependency` | Primary подтверждённо failed; переанализ не создан, фиктивного result нет |

Unknown, queued и недостающие обязательные attempts запрещают settlement и удерживают весь резерв. После разрешения всех зависимостей batch технически `completed`, только если все slots completed; иначе `failed`. При settlement освобождаются только неиспользованные attempts, например переанализ после failed primary. Старый single-run `Search.finish_selection` для такого selection запрещён.

Completed batch переходит в `awaiting_analysis`. Он не создаёт claim, gate approval или review. Несовпадение primary и reanalysis metrics может оставить технически completed batch с непрошедшим mechanical gate будущего claim. Failed исполнение не является опровержением гипотезы. Scientific validity во всех batch projections — `not_assessed`.

## Controller и восстановление

`episteme batch advance <batch-id> --root <directory>` последовательно восстанавливает roster из событий. Созданный slot повторно не enqueue. `work_job` разрешает один новый dispatch; старый dispatch только reconciles. Неизвестный исход останавливает продвижение, без автоматического retry. После появления исходного completion повторный advance импортирует результат и продолжает roster. Процессы не запускаются внутри SQL-транзакции. Конкурирующий stale command возвращает текущее состояние; для дальнейшего продвижения допустим новый вызов.

В `.execution-authority.json` лежит локальный случайный token; batch хранит только hash. Файл создаётся атомарно вне DB/CAS, не входит в backup и добавлен в gitignore. Admission slots, batch advance/settlement и persisted dispatch batch-bound job требуют соответствия token. Поэтому DB/CAS snapshot queued batch, сделанный до исполнения в оригинале, не получает права повторного запуска при restore. `execution work` также проходит этот dispatch guard. Чтение state/Graph/export работает без token. Проверка не создаёт и не заменяет отсутствующий/повреждённый marker.

Это локальная защита от случайного повторного запуска восстановленной истории, а не распределённый lease, host identity или аутентификация. Владелец filesystem может скопировать marker вместе с каталогом и создать второй исполняемый clone. Процедуры handoff/отзыва token пока нет; restored batch доступен для inspection, но автоматически не принимает новый execution ownership. Обычные jobs без batch сохраняют прежний контракт ADR 0005. Job workspaces/живые процессы не восстанавливаются из backup.

## Evidence и границы

Frozen reanalysis source/environment входят в artifact inventory уже при планировании. Выбранные search tree/node/selection, batch plan, slot bindings и settlement входят в соответствующий claim review basis; авторы batch и выбора эксперимента учитываются как contributors. Graph сохраняет ссылки на выбор, protocol, все slot runs/jobs и полное settlement. Изменение такого контекста требует актуального review. Legacy evidence basis без batch сохраняется.

Исполнение остаётся `trusted_local_python_v1`: отдельный cwd и контроль process group/Windows Job Object, без filesystem/network sandbox, environment reconstruction и полного resource ledger. Пример `examples/search_execution_batch.py` использует synthetic inputs, две реализации среднего и сохраняет невыбранный узел; останавливается до анализа и review. Реальные provider agents, автоматический analysis → review → replanning и manuscript pipeline остаются следующими этапами.
