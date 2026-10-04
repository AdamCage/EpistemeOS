# ADR 0012 — сохранённая выдача контекста и привязанный reviewer verdict

Дата: 29 сентября 2026. Статус: локально реализовано; независимость actor и процесса внешне не подтверждена.

## Проблема

`review.assign` из [ADR 0011](0011-review-assignment-context.md) сохранял предполагаемый контекст, но не фиксировал его фактическую выдачу и не связывал следующий `review` с назначением. После потерянного ответа провайдера повторный вызов мог создать второе, уже не сопоставимое мнение. Старые `kernel.review` и `replanning.record_review` остаются совместимыми локальными командами без assignment, поэтому наличие простого verdict в журнале нельзя считать доказательством независимого review. С шага 7 [ADR 0018](0018-claim-families-and-review-admission.md) `review.submit` — единственный путь записи approval, а решения засчитывают approval только из проверенной цепочки этого ADR.

## Контракт текущего среза

`ReviewerController.advance(assignment)` работает через доверенного локального planner и передаёт `ReviewProvider.invoke` только сериализованный запрос из frozen assignment manifest и bytes разрешённых `raw_data`/`metrics` CAS artifacts. Максимум — 256 artifacts, 32 MiB их суммарных bytes и 48 MiB сериализованного запроса. До вызова провайдера `review.dispatch` записывает один event/receipt и CAS-запрос. Сырой ответ размером не более 1 MiB записывается в CAS; `review.finalize` фиксирует его digest, status и usage отдельной receipt. Исключение или потеря завершения оставляет `unknown`; повторный `advance` **не** вызывает провайдера. `reconcile` требует явного решения оператора и не выполняет вызов.

`usage={}` означает отсутствие сообщённых счётчиков, а не нулевую стоимость. Подсчёт расходов и подлинность этих значений не удостоверены provider adapter.

Ответ в [review-response-v1.schema.json](../../schemas/review-response-v1.schema.json) содержит exact `assignment`, `bundle`, `claim`, `basis_hash`, `reviewer_actor`, verdict, rationale, findings и оценки связей claims. `review.submit` вызывается от reviewer actor через обычный [CommandService](../command-api.md) с `assignment`, digest завершённого `response` и `expected_basis`. Транзакция повторно проверяет assignment, доставленный ответ, actor/study, текущий пройденный mechanical gate и отсутствие reviewer среди contributors. Для `approve` findings должны быть пустыми; отрицательное решение сохраняет 1–32 typed открытых obligations. Одна receipt связывает `[review, review_obligation..., review_submission]` и hashes assignment, dispatch, response event, review и basis. Повтор той же команды возвращает исторический результат; второй verdict для одного assignment запрещён. Historical replay, Graph и review bundle проверяют связи и CAS bytes. Поздняя evidence revision не меняет исходную receipt, но делает положительный review устаревшим для следующего paper gate.

## Граница доверия

`identity_assurance=caller_declared` и `read_isolation=not_enforced` остаются точными значениями. Provider получает ограниченный аргумент функции, но Python-код под той же OS identity может прочитать Store напрямую; локальный planner также может вызвать `review.finalize` с произвольным CAS digest. `CommandService` не аутентифицирует actor ID, а JSON verdict не удостоверяет, что научную оценку выполнил отдельный субъект. Тестовые положительные ответы — только synthetic fixtures; они не являются научным approval. Существующий paper builder остаётся внутренним scaffold и не обозначает публикационную готовность.

Настоящая независимость требует отдельного write-service с аутентификацией до чтения receipts, отдельной OS/container identity или внешнего reviewer, принудительного allowlist файлов/сети и отрицательных canary-тестов доступа. Также остаётся работа над автоматическим batch → analysis/claim → reviewer → replanning циклом и предметной проверкой качества review. Этот ADR фиксирует лишь воспроизводимость доставки и привязку записанного мнения к evidence revision.
