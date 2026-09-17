# ADR 0004: версии вопроса и конкурирующих объяснений

Статус: реализованный локальный контракт M1, 17 сентября 2026. [Архитектура](../architecture.md), [MVP-план](../mvp-plan.md), [результаты проверки](../validation.md). Этот инкремент связывает планирование с существующими protocols; генерация гипотез и выполнение исследования агентами остаются дальнейшими этапами.

## События и допуск

`Planning.question` создаёт `research_question`: `schema_version=1`, `study_id`, statement, objective, точный scope, непустые списки constraints и stopping criteria. Это декларации. Численные бюджеты, domain-pack version, доступ к данным и фактическое выполнение stopping rules здесь не реализованы.

Изменение создаёт новое событие с `parent`, `parent_hash` и обязательным `revision_reason`. Parent должен быть текущей head своей lineage и принадлежать тому же study. Root требует null parent/hash/reason. Вопрос можно уточнить, в том числе изменить scope; прежняя версия остаётся доступной. Два writers не могут одновременно записать продолжение одной head: запись использует expected revision проверенного snapshot.

`Planning.explanation_set` связывает текущую версию вопроса минимум с двумя существующими уникальными hypotheses. Scope каждой должен точно совпадать со scope вопроса. Payload сохраняет question ID/hash, hypothesis IDs/hashes, study, comparison plan и revision lineage. Comparison plan описывает различающий тест; непустой текст не доказывает научную различимость объяснений.

Revision набора требует текущую head родительского набора, ту же question lineage и study. Она может перейти к новой версии своего вопроса. `excluded_reasons` должен содержать ровно IDs удалённых из parent кандидатов, с непустой причиной для каждого. Старые hypotheses, pools, protocols и неудачные попытки не изменяются. Несколько самостоятельных наборов для одного вопроса допускаются; root другого набора не объявляется revision без parent-ссылки.

Публичные payload schemas: [ResearchQuestion v1](../../schemas/research-question-v1.schema.json), [ExplanationSet v1](../../schemas/explanation-set-v1.schema.json). Runtime дополнительно проверяет prior refs, hashes, scope, lineage heads и причины исключений. Pure helpers в [planning.py](../../src/episteme/planning.py) работают с уже проверенной event history; сами не удостоверяют её подлинность или научную правильность.

## Frozen protocol binding

Новый `Kernel.preregister_for_set` принимает explanation set ID и параметры эксперимента. Поля hypotheses и scope выводятся из набора и вопроса; caller не передаёт расходящуюся копию. В том же protocol event, до первого run, сохраняется:

```json
{
  "planning": {
    "schema_version": 1,
    "study_id": "study-id",
    "question": "question-event-id",
    "question_hash": "<SHA-256>",
    "explanation_set": "set-event-id",
    "explanation_set_hash": "<SHA-256>"
  }
}
```

При регистрации набор и вопрос должны быть текущими heads. Проверка повторяется на историческом prefix перед событием при чтении protocol, запуске run и проверке evidence. Поздняя revision планирования не переписывает исходный protocol и не делает его исторический binding повреждённым.

Новый protocol с parent, уже имеющим planning binding, обязан сохранить study и question lineage; сброс в legacy protocol запрещён. Legacy parent можно продолжить новым явно связанным protocol. Statistical design, exposure, seeds, blobs и остальные preregistration gates сохраняются. Planning revision сама по себе не создаёт новый run, claim или approval.

Сигнатура/defaults опубликованного `kernel.preregister` v1 не изменены. Старые protocols не получают выдуманный question/set задним числом. Добавлены отдельные command actions `planning.question`, `planning.explanation_set` и `kernel.preregister_for_set`; исторические receipts продолжают возвращать исходный результат после появления новых heads.

## Evidence context и reviewer

В basis bound claim входят выбранный набор, его ancestors, привязанные к ним версии вопроса с ancestors и все hypotheses из этих наборов, включая исключённые. Их авторы считаются contributors и не могут выполнять review соответствующего результата. Это распространяется и на planning context связанных claims и учитываемых foreign protocols.

Поздние descendants и unrelated lineages не входят в frozen context. Поэтому уточнение будущего вопроса не отзывает approval старого результата в его прежнем scope. Новое исследование использует новый protocol и собственные claim/review; approval исходной версии ему не наследуется.

Graph содержит типизированные planning nodes, parent/question/hypothesis refs, protocol binding и review context. Bundle сохраняет все версии. Paper scaffold показывает выбранную planning ancestry, hypotheses с prediction/falsifier и причины исключений. Это след происхождения вывода, а не подтверждение качества плана или новизны результата.

## Study metadata и границы доверия

Внутри command transaction `context.study_id` сверяется с вопросом и явно bound references. Проверка действует также при использовании старых action names для bound runs, claims, reviews, paper и tree selections. Tree получает проверяемую связь через protocols его nodes. Повтор уже принятой команды возвращается из receipt до новой admission-проверки; он не получает права на новый переход.

Legacy records без planning binding не получают неявного study. Их hypotheses можно явно включить в новый набор без изменения исходных hashes. Один command context не может приписать связанные записи другого явно заданного study своему study. Это согласованность metadata, не аутентификация или access boundary: direct Python API, filesystem, actor identity и глобальный exposure ledger сохраняют прежнюю доверенную локальную модель.

## Приёмка и оставшееся

Tests проверяют строгие payloads, scopes и hashes; revisions текущих heads; причины удаления; конфликт writers; historical binding без future descendants; protocol amendment/downgrade; command replay после смены heads и отказ при study mismatch, включая search selection; reviewer conflicts всей ancestry; forged Graph refs; реальный CLI и backup/restore с исходными receipts. Opinions и outputs в этих tests — явно обозначенные fixtures, не scientific review реального исследования.

Остальные entity schemas, общий migration framework и сопоставление statistical plan с фактическими outputs остаются открытыми. M2 отвечает за исполнение, resource accounting, leases/recovery и sandbox; M3/M4 — за агентов, разнообразие объяснений, научную оценку и автоматический replan.
