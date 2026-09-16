# ADR 0003: связи claims и отзыв актуальности review

Статус: **реализованный контракт локального инкремента M1** от 10 сентября 2026; окончательная сверка ADR с исходниками — 11 сентября 2026. Основания: [архитектура](../architecture.md), [MVP-план](../mvp-plan.md), текущие `claims.py`, `claim_context.py`, `kernel.py`, `graph.py` и `reporting.py`. Этот контракт не означает завершение M1, независимое научное review или готовность manuscript к подаче. Сценарии проверки перечислены ниже; сводные результаты ведутся в [validation.md](../validation.md).

## Назначение и API

Событие `claim_link` сохраняет заявление автора о связи двух claims. Оно не устанавливает поддержку или опровержение, не заменяет replication и не превращает tournament priority в истинность. Ядро сохраняет контекст, обнаруживает изменение доказательной основы и требует явного review перед допуском claim в paper.

Pure [ClaimLink v1](../../src/episteme/claims.py) и [JSON Schema](../../schemas/claim-link-v1.schema.json) задают payload:

```text
{
  schema_version: 1,
  source, target,
  relation: supports | contradicts | limits | supersedes,
  rationale,
  source_hash, target_hash,
  source_basis, target_basis
}
```

`Kernel.link_claims(source=..., target=..., relation=..., rationale=..., expected_bases={source: ..., target: ...})` доступен ролям `planner` и `analyst`. Оба endpoint должны быть существующими различными claims в точно совпадающем scope. `source_hash/target_hash` фиксируют события claims; `source_basis/target_basis` — контекст непосредственно перед добавлением связи. Изменение истории между проверкой и записью вызывает revision conflict. Через `CommandService` допуск, событие и receipt проходят в одной короткой транзакции; прямой вызов Kernel сохраняет optimistic revision check без обещания идемпотентной доставки.

Pure contract проверяет форму и значения полей. Разрешение ссылок, hashes, basis, scope и циклов выполняют resolver и Kernel. Ссылка на raw result сначала требует собственного bounded claim; добавление relation не создаёт observations. Позднее evidence сохраняет связь как историческое заявление: admission basis проверяется по prefix до события, а нынешняя оценка использует обновлённый контекст.

## Направления и циклы

| Relation | Значение source → target | Review context |
|---|---|---|
| `supports` | source заявлен как поддержка target | target включает source и его входящие зависимости |
| `limits` | source заявляет ограничение target | target включает source и его входящие зависимости |
| `contradicts` | source и target заявлены как конфликтующие | Оба endpoint включают друг друга |
| `supersedes` | source — предлагаемая новая версия, target — старая | Оба endpoint включают историю друг друга |

`supports/limits` образуют общий directed acyclic graph. `supersedes` имеет отдельную lineage с обязательным `source.seq > target.seq`, исключающим обратную и циклическую замену. Поэтому `old supports new` вместе с `new supersedes old` допустимы: смешанный обход review context не является циклом dependency DAG. Симметричные contextual relations не добавляются в проверку циклов `supports/limits`.

Повтор той же пары и relation запрещён; для `contradicts` перестановка endpoints также считается повтором. Разные relations между одной парой могут сохраняться отдельно. Ни relation, ни номер события не выбирают победителя научного спора и не удаляют предыдущую версию.

## Транзитивный контекст и версии basis

Общий pure `resolve_context(history, claim)` используется Kernel и Graph на переданном snapshot. Он начинает с выбранного claim, добавляет sources входящих `supports/limits`, затем противоположные endpoints `contradicts/supersedes` и повторяет обход до неподвижной точки. Все применимые links сохраняются, включая дополнительные связи между уже найденными endpoints. Claims и links возвращаются в порядке событий.

Для `A supports B`, `B supports C` новое evidence A меняет basis B/C. Unrelated D и изменение только downstream C не меняют basis A. Contradiction и supersession распространяют контекст в обе стороны; replacement не может скрыть новую проблему старого claim. `rejected` assessment не удаляет связь из замыкания: новое evidence отвергнутой альтернативы тоже требует переоценки её отклонения.

Без links прежний canonical basis сохраняется: собственный protocol, claim, его runs/results и применимые exposure records. Для linked context используется объект `basis_version=2` с ID рассматриваемого claim, отдельным local evidence bundle каждого claim замыкания, событиями links и `context_findings`. В local bundles входят текущие runs/results, включая failed attempts, и применимые exposure records. Метрики, seeds и coverage разных protocols при этом не объединяются.

`source_basis/target_basis` вычисляются **до** добавления link: сама связь закономерно меняет basis одного или обоих endpoints. Это не делает сохранённые admission hashes повреждёнными. Kernel и Graph проверяют historical links/reviews/papers по соответствующим prefixes; текущие gates и paper eligibility вычисляются по нынешнему snapshot.

## Собственная и связанная mechanical readiness

Gate рассматриваемого claim требует его **текущего собственного** mechanical pass, корректных link bindings и доступности проверяемых bytes всего связанного контекста. Проверяются текущие code/environment/data/raw/log/output artifacts, в том числе относящиеся к поздним и неуспешным попыткам. Противоречие само по себе не устанавливает `gate.passed=False`, `outcome=refutes` или научную независимость evidence; `scientific_validity` остаётся `not_assessed`.

Для остальных claims gate отдельно возвращает `related_claims` с двумя результатами:

- `historical_gate`: локальные условия на prefix к моменту создания этого claim, с проверкой доступных сохранённых bytes;
- `current_gate`: локальные условия на нынешнем snapshot.

Эти результаты не подменяются одним boolean и не агрегируются как обязательный current pass всех источников. Старый claim может корректно описывать прежние runs, но не включать новые завершённые runs того же protocol в свои immutable citations. Его current gate тогда не проходит. Требование current pass такого старого контекста навсегда заблокировало бы корректный replacement claim, который уже ссылается на полный набор результатов.

Для `approve` с `judgment=accepted` source каждой связи должен пройти выбранную локальную проверку. Для `supports/contradicts/limits` используется gate на момент создания source claim. В accepted supersession lineage выделяется активный replacement frontier: источники accepted `supersedes`, которые сами не являются target другой accepted `supersedes` в этих assessments. Для них требуется **текущий локальный gate**; внутренние звенья lineage проверяются исторически. Историческая готовность источника не означает его нынешнюю готовность или scientific validity.

Для прямой замены `B supersedes A` текущая готовность B обязательна. Для цепочки `C supersedes B`, `B supersedes A`, где обе связи accepted, она обязательна для C; B может оставаться историческим промежуточным claim того же protocol. Если `C supersedes B` rejected, B снова входит в активный frontier и его current gate обязателен. Это позволяет сохранять несколько последовательных версий без вечной блокировки на неполных относительно новых runs citations промежуточной версии.

Предложенную связь от механически неподготовленного source можно явно `rejected` при review исправного target, если весь контекст доступен. Source при этом не получает pass, approval или независимую replication. Так можно отклонить слабую поддержку без необходимости сначала признать её источник полноценным результатом. Отсутствующие или повреждённые bytes всё равно блокируют review, включая review с намерением отклонить связь.

## Явная оценка связей

`Kernel.review_with_links` создаёт `review` event с `review_schema_version=2`. Сигнатура/defaults прежнего `review` v1 не изменяются; это сохраняет опубликованный normalization contract command receipts. Старый метод отклоняет **любой** verdict для linked context и требует новый action.

`link_assessments` — mapping, покрывающий ровно все links текущего замыкания:

```text
{
  link_id: {
    judgment: accepted | rejected | unresolved,
    disposition: compatible_as_written | requires_claim_revision | needs_evidence,
    rationale,
    evidence: [context_event_id, ...]
  }
}
```

Каждый assessment требует непустого объяснения и уникального непустого списка evidence refs. Ссылки должны принадлежать разрешённому review context: его claims/protocols/hypotheses/runs/results/exposure records, links либо связанным эпизодам review findings. Неизвестные, посторонние IDs и отсутствующие assessments отклоняются.

`approve` допускается при отсутствии `unresolved`, при `compatible_as_written` для каждого link, без открытых actions и с выполненными проверками source readiness. Отрицательный verdict требует actions. `judgment` описывает оценку relation, `disposition` — возможность оставить рассматриваемый claim в текущей формулировке. Поэтому accepted contradiction может сопровождать явно ограниченный или inconclusive claim, если reviewer объяснил совместимость. Ядро проверяет наличие решения, ссылки и readiness; смысл и достаточность такого объяснения оно не доказывает.

## Собственное veto и чужие review findings

Собственные reviews не входят в evidence basis своего claim: запись review не должна немедленно инвалидировать себя. `next_action` учитывает последний verdict каждого reviewer **по всей истории этого claim**, независимо от basis. Неотозванный `request_changes/reject` сохраняет veto после нового evidence или relation. Положительный голос другого actor его не закрывает. Для снятия собственного veto в текущем контракте нужен новый допустимый положительный review того же actor; paper дополнительно требует актуального review basis.

Отрицательные reviews **других claims** из замыкания входят в `context_findings` рассматриваемого claim. `_context_findings` сохраняет по паре `(claim, reviewer)` каждый отрицательный review и первое следующее положительное решение того же actor, которое закрывает этот эпизод. Если отрицательное решение обновлено до закрытия, все его версии остаются в records; открытым считается последнее. Повторное отрицательное решение после закрытия начинает новый эпизод. Закрытые эпизоды не исчезают из basis, поэтому ранее отозванное approval не может снова стать актуальным из-за удаления finding из текущего списка открытых замечаний.

Обычные чужие положительные reviews, которые не закрывают отрицательный эпизод, в basis не добавляются. Это предотвращает бесконечное взаимное устаревание approvals у симметрично связанных claims: закрытие finding может потребовать переоценки соседнего claim, но последующие обычные approvals не вызывают новый цикл.

Gate раскрывает `open_context_reviews`. Перед `approve` объединение `evidence` всех link assessments должно явно включать IDs каждого открытого чужого review. Это проверка явного ознакомления с зарегистрированными замечаниями, а не автоматическое доказательство их разрешения. Например, reviewer B может объяснить ограниченный вывод B при сохраняющемся finding к A; review B **не закрывает veto A**, не меняет его verdict и не удаляет finding. Исходный claim продолжает требовать решение владельца своего veto. Семантическая достаточность ответа, сопоставление каждого замечания конкретному исправлению и проверяемое closure обязательств остаются задачами M4.

## Независимость и admission guard для владельца veto

Reviewer не может быть автором claim/protocol/hypothesis/attempt либо иных учитываемых результатов полного рассматриваемого контекста, а также автором оцениваемого link. Чтение evidence через `data_exposure` и авторство отдельного scientific review сами по себе не делают actor автором экспериментального результата.

`link_claims` проверяет всех владельцев открытых veto в затрагиваемых contexts. Новый link отклоняется, если его добавление превратит такого reviewer в contributor контекста и тем самым отнимет право на требуемый независимый повторный review. Проверка охватывает не только собственное авторство link: независимый автор связи тоже не может добавить evidence участника, который уже владеет veto на target. Это guard допуска links, а не универсальная система управления конфликтами ролей.

Actor IDs и роли задаёт доверенный caller. Изменение role label не доказывает независимости, а проверки IDs не аутентифицируют пользователя, не изолируют context и не ограничивают доступ к файлам. OS sandbox, authenticated assignments и реальные независимые agent sessions этим инкрементом не реализованы. Meta-review, evidence-based obligations и их closure остаются M4; сам факт записи fixture review не является выполнением научного review.

## Supersession и paper

Порядок `next_action`: собственная mechanical failure или нарушение artifact closure → `repair_evidence`; сохраняющееся собственное отрицательное review → `replan`; отсутствие current-basis review → `scientific_review`; затем проверка supersession и paper eligibility.

При актуальном accepted `supersedes` в последней оценке хотя бы одного reviewer старый target возвращает `superseded` и не допускается как current paper anchor. Используется последняя оценка каждого reviewer на текущем basis: его последующий явный `rejected` не оставляет собственный прежний accepted verdict действующим. Пока другой актуальный reviewer сохраняет accepted replacement, исторический target не возвращается в anchors. Rejected replacement может вернуть обычную eligibility после выполнения остальных gates и review условий.

Новый replacement не наследует approval старого claim. Ему нужны собственный current gate и review полного контекста. Если старый target не проходит собственный current gate из-за расширившегося набора runs, он остаётся историческим неподготовленным anchor; это не блокирует допустимую оценку нового claim с сохранённым старым контекстом. Ни supersession, ни исключение из anchors не удаляют старые результаты, ограничения или reviews.

`PaperBuilder.build/materialize` перепроверяют `next_action` и basis на snapshot. Manuscript сохраняет relevant relations с rationale, competing/prior claims с limitations и таблицами их текущих runs, актуальные assessments выбранных claims и связанные review episodes. Эти contextual claims явно отделены от выбранных approved anchors. Evidence bundle содержит полную историю и `claim_relations`, а `selected_context` перечисляет использованные claims/links и review findings; inclusion не является promotion.

Изменение evidence, link либо foreign negative episode делает старый paper историческим и блокирует его нынешнюю materialization до новой допустимой сборки. Command receipt возвращает исторический ID, не повторный scientific approval. Результат остаётся внутренним evidence-linked scaffold; human release, полноценные manuscript/venue проверки и внешнее peer review впереди.

## Проверки и границы результата

Контракт покрывается [pure ClaimLink tests](../../tests/test_claims.py), [resolver tests](../../tests/test_claim_context.py), [workflow fixtures](../../tests/test_claim_workflow.py) и [регрессией многоступенчатой lineage](../../tests/test_claim_lineage.py). В частности:

- транзитивное изменение basis B/C от A, независимость D, historical Graph и отзыв старого paper;
- симметричный contradiction context без автоматического scientific approval, сохранение competing claim в manuscript;
- stale admission basis, concurrent append, scope mismatch, cycles и более старый superseding source;
- missing/extra/out-of-context assessments, unqualified source и сохранение failed-attempt artifacts;
- separate historical/current readiness и same-protocol `old supports new` + `new supersedes old`;
- запреты review для contributors/link authors и guard всех затрагиваемых владельцев veto;
- сохранение собственного veto между basis, обязательное acknowledgement чужих findings без их закрытия и конечная переоценка симметричных review episodes;
- accepted/rejected supersession, current readiness активного replacement frontier, historical readiness промежуточной версии, command replay и проверка исторических link hashes/bases.

Outputs, relations и opinions в этих сценариях являются явно обозначенными fixtures. Проверки подтверждают локальные переходы и сохранность записанного контекста, не научную корректность relation или текста review. Полное типизированное описание остальных M1 сущностей, authenticated execution, статистические/domain checks, независимые агенты, obligations/replanning и manuscript pipeline сохраняют отдельные критерии приёмки в MVP-плане.
