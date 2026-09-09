# ADR 0002: statistical design и история просмотра данных

Статус: **локальный контракт реализован**, 9 сентября 2026. [StatisticalDesign](../../src/episteme/protocols.py), [schema v1](../../schemas/statistical-design-v1.schema.json), [Kernel](../../src/episteme/kernel.py) и [Graph](../../src/episteme/graph.py). Основания: [архитектура](../architecture.md), [приёмка M1](../mvp-plan.md). Проверка деклараций не устанавливает научную корректность метода, мощность исследования или независимость наблюдений.

## Что замораживается

`Kernel.preregister(statistical_design=...)` принимает типизированную декларацию: `mode`, experimental unit, estimand, primary/secondary metrics с units, sample size и rationale, uncertainty method/unit/rationale, exclusions, stopping rule, multiple-testing family/correction/rationale и data splits с SHA-256/role/exposure policy. `sample_size` означает число заявленных экспериментальных единиц, а не число seeds или строк в файле.

Primary metric и stopping rule должны совпадать с общими полями protocol. Имена metrics и split IDs/digests уникальны. Заявленная resampling unit должна совпадать с experimental unit; это обнаруживает явное противоречие деклараций, но не скрытую зависимость реальных данных. Отсутствие uncertainty/significance test задаётся явным `not_applicable` и причиной. Числа sample-size/power и содержание методик пока не пересчитываются универсальным ядром; это работа domain adapter и scientific review.

Режим — `descriptive`, `exploratory` или `confirmatory`. Confirmatory требует зарегистрированного confirmatory split с holdout/new-data policy либо sequential policy со stopping/error-control declaration. Sequential policy сама по себе не разрешает повторно назвать уже просмотренные данные незатронутыми: первая локальная версия консервативно требует новых bytes. Более сложные sequential/cluster designs нуждаются в отдельном adapter policy.

## Exposure и amendments

`Kernel.expose_data(data=digest, purpose=..., protocol=None)` сохраняет, кто заявил о просмотре bytes и зачем. Это declarative ledger; `Store.read` не становится автоматически журналируемым или запрещённым. Доступ к файлам, просмотры вне системы, эквивалентные копии с другим digest и фактическая независимость участников не устанавливаются.

При регистрации typed protocol `seen_data` объединяет явный список caller, ранее зарегистрированные exposures/seen_data, inputs/splits уже начатых runs и сохранённые raw outputs, включая failed attempts. Учёт охватывает весь локальный Store. Удаление parent из нового запроса не позволяет переименовать использованные discovery bytes в свежий holdout.

Typed amendment имеет новый ID, неизменяемого parent и обязательный `amendment_reason`; parent inputs/splits считаются просмотренными консервативно. Новый typed protocol хранит snapshot `seen_data`. Сброс typed parent в legacy protocol запрещён. Для нового confirmatory split digest не должен входить в этот snapshot. Исходная preregistration и все прежние outcomes остаются в истории.

У claim сохраняется `inference_mode`: по умолчанию режим typed protocol. Confirmatory claim нельзя получить из exploratory/descriptive/legacy protocol. Старый API без statistical design остаётся поддержанным, прежние payloads/hashes не дополняются задним числом. Legacy inference имеет статус `unclassified`; это не подтверждённый научный дизайн.

## Review basis и mechanical gates

Для typed claim immutable basis включает исходный protocol, claim, все собственные runs/results и релевантный exposure context. Последний содержит явные просмотры связанных данных, foreign attempts на тех же inputs/splits, их results, связанные protocols и новые релевантные seen-data declarations. Наследование уже известного `seen_data` другим планом не добавляет нового просмотра. Не связанное с данными исследование не инвалидирует basis.

Новая запись в этом контексте требует нового review; старый verdict сохраняется как исторический. Плановый просмотр после frozen protocol не является сам по себе провалом механики. Failed foreign attempts остаются контекстом review и не превращаются в успешные результаты основного claim. Gates проверяют bytes всего добавленного exposure context. Автор связанного foreign protocol/attempt не может принять роль reviewer собственного evidence; обычное чтение bundle рецензентом допустимо.

Graph и Kernel используют один алгоритм basis. Graph проверяет typed declarations и preregistration timing на историческом prefix, строит связи к splits, seen-data, exposure и review; Reporting включает эти bytes в artifact inventory. Claims сохраняют `scientific_validity=not_assessed` независимо от механического прохождения, режима или знака эффекта.

## Граница текущей реализации

Зарегистрированные sample size, units, multiplicity и stopping требуют дальнейшего сопоставления с фактическими outputs. Сейчас нет проверки распределения данных, расчёта uncertainty, p-values, alpha spending, power, эквивалентности samples по содержанию или соответствия raw output заявленному split. Fixture с нулевым эффектом проверяет сохранение outcome и pipeline, а не научное подтверждение null hypothesis. Общие contradiction/supersession links и authenticated data access остаются незавершёнными требованиями M1/M4.
