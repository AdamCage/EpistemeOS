# ADR 0014 — ручная привязка frozen domain recipe

Дата: 3 октября 2026. Статус: реализовано и локально проверено; результаты проверок указаны в [validation.md](../validation.md).

## Проблема

В [ADR 0013](0013-batch-analysis-admission.md) анализ допускался только для протокола, созданного через `agent_application`. Ручной `followup.apply` уже мог создать и исполнить дочерний protocol, но затем не мог передать завершённый batch в `analysis.apply`. Исторические Afterlife данные также нельзя было связать с исполненным офлайн-анализом через этот переход. Само наличие protocol data и двух различных файлов кода не фиксировало предметный recipe и выбранную реализацию reanalysis до запуска.

## Решение

`domain.bind` — команда planner в `CommandService`, которая до любого run или batch plan связывает planning-bound protocol с ID/версией предметного адаптера, bytes его исходника, ограниченным JSON recipe, дополнительными CAS artifacts и точными параметрами будущего batch: отдельной реализацией и environment reanalysis, outputs, wall time, лимитом bytes и capabilities. У protocol уже frozen primary code, environment и data; привязка сохраняет их digests и hash protocol. Domain recipe остаётся предметным: ядро проверяет только форму, существование CAS bytes, совместимость с runner и study. Оно не оценивает научные предпосылки recipe.

Один protocol имеет не более одного источника recipe: `agent_application` либо `domain_binding`. Receipt и event проверяются на историческом префиксе, включая отсутствие предшествующего run/batch plan. `batch.plan` повторно сравнивает исполняемый recipe с привязкой **до запуска**, а `analysis.apply` — перед commit bounded claim. Адаптер анализа обязан отдельно проверить смысл domain recipe, сырые наблюдения, roster и числовую метрику. Для synthetic fixture это уже реализовано: адаптер читает зарегистрированные параметры ручной привязки, сравнивает их с наблюдёнными raw rows и независимо пересчитывает point estimate. Он не читает скрытый `world` из protocol data.

`domain_binding` включён в claim evidence basis, mechanical CAS checks, Graph, artifact inventory и review bundle. Reviewer context не получает recipe или исходник адаптера. Повреждение привязанных bytes останавливает проверку, а изменение evidence требует нового review basis. Исполнитель, аналитик и reviewer всё ещё являются caller-declared actor IDs одного локального контекста; привязка не удостоверяет авторство, изоляцию ОС или научную независимость.

## Ограничения и следующий шаг

Этот переход делает возможным анализ ручного follow-up и второго domain pack, но сам не создаёт Afterlife claim. Для него нужно захватить все сырые bytes выбранного run с полной проверкой заявленных hashes/roster, сохранить новый **exploratory** protocol с `seen_data`, написать предметные первичный и отдельный пересчёт и проверить результат на реальном локальном пилоте. Исторический PLAN не становится preregistration задним числом. Никакое reviewer approval, новый вызов модели, провайдера или публикация этим переходом не создаются.
