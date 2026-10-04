# ADR 0011 — назначение reviewer и замороженный контекст

Статус: реализован локальный контракт контекста; аутентификация и изоляция чтения не реализованы. 28 сентября 2026.

## Проблема

Проверка разных строковых actor ID предотвращает очевидное self-review, но не определяет, какие материалы получил reviewer. Она не доказывает, что другой агент, процесс или человек не видел исходный код, авторский отчёт или прежние verdicts. Для последующей независимой проверки нужен сохраняемый и проверяемый след назначения, не выдающий декларацию за фактическую границу доступа.

## Решение

Planner вызывает `review.assign` для текущего claim, указывая `reviewer_actor` и точный `expected_basis`. Команда требует пройденный mechanical gate, сверяет текущий immutable basis и отказывает actor ID, участвовавшему в создании evidence context. Одна command receipt фиксирует ровно одно событие `review_assignment`; событие связывает claim/event hash, basis, study, reviewer, policy и digest JSON manifest в CAS. Повторная доставка исходной команды возвращает историческую receipt. Для нового назначения нужен новый command ID и актуальный basis.

Policy `blind_initial_review_v1` строит явную проекцию: вопрос, competing explanations, hypotheses, claim и связи, зарегистрированный protocol со статистическими допущениями, метаданные наблюдённых runs/results. Список разрешённых blob digests содержит только наблюдённые `raw_data` и `metrics`; коллизия с исходным implementation, environment либо иным output отвергается. Direct digests исходной и повторной реализации, environment, logs и ещё не наблюдённых protocol data в manifest не помещаются. Авторские отчёты и прежние reviews не входят в эту проекцию. Свободный текст исходных records не очищается от смысловых подсказок: policy ограничивает поля и ссылки, а не гарантирует семантическую слепоту.

Исторический replay повторно строит manifest на префиксе до назначения, проверяет bytes/digest, event payload, роль и единственную receipt. Graph добавляет target, context и разрешённые artifact edges. `review.assign` не создаёт review, verdict или право публикации; существующие `kernel.review` и `replanning.record_review` такого назначения не требуют. С шага 7 [ADR 0018](0018-claim-families-and-review-admission.md) они записывают только отрицательные мнения, а approval засчитывается лишь из цепочки назначения, выдачи и `review.submit`.

## Граница доверия

Событие явно записывает `identity_assurance="caller_declared"` и `read_isolation="not_enforced"`. Actor ID вводит доверенный caller; `CommandService` его не аутентифицирует. Manifest является спецификацией того, что **следует** дать reviewer, а не ограничителем `Store.read`, доступа к каталогу, сети или другому контексту модели. Локальный Python reviewer под той же учётной записью может читать остальные файлы. Текущий Windows Job Object контролирует жизнь дочерних процессов, но не права на файлы или сеть. Разные role labels и implementation hashes не доказывают независимость исполнителя, повторного анализа или научной оценки.

Для настоящего независимого review нужен единственный write-service с проверенной identity до поиска command receipt, отдельное read-only представление разрешённых bytes и техническая граница доступа (отдельная OS identity/container с проверенными filesystem/network capabilities либо внешний reviewer без доступа к Store). После этого review command должен быть связан с назначением и его bundle; сегодня такого обязательного перехода нет.

## Следствия и проверка

Контракт сохраняет отрицательные findings, исходные preregistrations и конкурирующие объяснения; назначение не меняет scientific validity, tournament priority или execution success. Тесты проверяют stale basis, очевидный contributor conflict, исключение source/log/holdout digests, exact receipt/replay, Graph и backup/restore. Эти synthetic fixtures проверяют механический контракт, но не удостоверяют независимое мнение reviewer.
