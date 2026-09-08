# Независимая проверка локального MVP

Дата: 2026-09-07. Проверена текущая рабочая папка `C:/Projects/EpistemeOS`, Python 3.11.15, Windows. Проверка относится к `kernel.py`, `store.py`, offline demo, CLI и появившемуся во время проверки модулю `reporting.py`. Исходный код reviewer не менял; исправления реализации выполнял основной агент. Проверяющий добавил поведенческие тесты и этот отчёт. Это технический review прототипа, а не Scientific Reviewer заключение по исследовательским результатам.

Проверенный результат — работающий локальный контракт evidence → mechanical gate → внешне подготовленный review → внутренний черновик. Автономная научная система и качество публикации уровня TMLR/ICLR этой проверкой не подтверждаются.

Выполнена команда из корня репозитория:

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

На момент независимого запуска: **54 теста, 30.885 секунды, OK** — 39 тестов kernel и 15 CLI. Тесты, добавленные другими агентами после этого запуска, в этот результат не входят. Воспроизвести проверенный набор отдельно можно командой `python -m unittest tests.test_kernel tests.test_cli -v` с тем же `PYTHONPATH`.

Также выполнен отдельный настоящий `python -m episteme demo --root <unique-temp-root>`. Он создал две гипотезы, один preregistered protocol, шесть run/result пар и один claim: 16 событий, без review и paper. После закрытия процесса команды `inspect`, `gate` и `export` повторно открыли состояние, вернули exit code 0 и не изменили историю. Gate вернул `passed=true`, `scientific_validity=not_assessed`; next action — `scientific_review`.

| Seed | Primary OLS slope | Reanalysis на тех же наблюдениях |
| --- | ---: | ---: |
| 17 | 2.073857305222232 | 2.0738573052222318 |
| 41 | 2.0069078315857105 | 2.00690783158571 |
| 73 | 2.118016697757309 | 2.118016697757309 |

Разница каждого значения укладывается в preregistered absolute tolerance `1e-10`. CLI-тест дополнительно выполнил второй полный demo в другой папке и сравнил байты raw data и metrics по seed и типу вычисления: совпадают на текущем Python runtime. UUID, timestamps, PID, runtime metadata и полная история разных запусков не обязаны совпадать.

Локальный след отдельной проверки сохранён в `C:/Users/yytrb/AppData/Local/Temp/episteme-independent-review-8a3dc326af794f299812df6ab9ac361d`. Это временная папка для аудита текущей сессии, не постоянный артефакт репозитория. Claim: `claim-c88baf62f10346ed`; последний event hash: `c39cdb73db410ae65d48bd306f3dc29da8f1946b315c52a250bff08946ed9b87`; basis hash: `511ed2ed2621133289fa8a3c7d4753191e3f37f0b66021e8bae5125c3ead2858`. Основной способ повторной проверки — новый demo и тесты, а не наличие этой временной папки.

| Проверяемое свойство | Наблюдаемое подтверждение | Граница утверждения |
| --- | --- | --- |
| Preregistration | Run ссылается на ранее созданный protocol hash; незарегистрированный seed и изменённые primary implementation/environment отвергаются | Natural-language design, stopping rule и analysis plan не исполняются и не проверяются семантически |
| Бюджет и конкурентные writers | Устаревший writer получает ConflictError; одновременные start_run не превышают run_limit | Проверен небольшой конкурентный сценарий; нет распределённого scheduler и cluster consistency |
| Evidence completeness | Gate требует завершённые зарегистрированные seeds, все завершённые runs и reanalysis каждой successful primary; незавершённый run блокирует переход | Failed/cancelled runs сохраняются, но допускают успешную повторную попытку в рамках числового бюджета |
| Provenance integrity | Missing/corrupt artifacts блокируют gate/export; проверены также зарегистрированные логи failed/cancelled попыток | Kernel доверяет тому, что поданные caller артефакты описывают действительно исполненную команду |
| История | SQL triggers запрещают update/delete; изменение actor/payload после обхода trigger обнаруживается по hash chain | Полная корректно пересчитанная подмена или усечение suffix требуют внешнего trusted checkpoint для обнаружения |
| Перезапуск | История, артефакты, basis, gate и мнение reviewer сохраняются после reopen | Это сохранность локальной SQLite/CAS модели; backup/restore, crash recovery и filesystem failure здесь не доказаны |
| Review freshness | Изменение relevant run/result bundle делает review устаревшим; отрицательный reviewer veto сохраняется при чужом approval | Snapshot observation не удерживает блокировку против будущих событий; downstream writer обязан повторно проверять состояние |
| Export | Bundle содержит history, snapshot hash и проверяемый manifest; повторный экспорт неизменного состояния даёт те же байты | Три производных файла заменяются по отдельности, не одной filesystem transaction; embedded bundle history — основание конкретного snapshot |
| Review → paper CLI | Без review и после request_changes paper отклонён; актуальное approval позволяет сохранить CAS manuscript/bundle и paper event | В тесте review opinion задан fixture JSON; тест не имитирует независимое научное мышление |
| Ошибки CLI | Missing state не инициализируется; ошибки gate/review/export имеют ненулевой exit code и структурированный JSON | `inspect` сообщает gate failures внутри JSON; успешный inspect сам по себе не означает успешный gate |

Найденные во время проверки расхождения:

- Старый тест `next_action` подменял публичный `gate`, хотя реализация уже использовала `_gate` с общим history. Это был устаревший тест, а не доказательство product race. Он заменён проверкой одного history snapshot, его передачи в `_gate` и наблюдения последующего run при следующем вызове.
- Export первоначально строил history, summary, gates и JSONL из разных snapshots. Основной агент вынес reporting в отдельный модуль с общим history. Статически проверен общий event snapshot; в CLI проверены matching event history/snapshot и детерминированный повторный экспорт. Детальный конкурентный reporting test относится к набору основного агента.
- Export первоначально предполагал метрику `slope` для любого протокола. Реализация теперь читает зарегистрированную primary metric и показывает её имя; это проверено чтением текущего `reporting.py`. CLI numerical fixture использует `slope` и сама по себе не является проверкой произвольной метрики.
- Gate первоначально пропускал integrity checks для outputs неуспешных попыток. Удаление failed-run log после успешного retry давало `passed=true`. Основной агент перенёс проверку всех outputs до ветки failed/cancelled. Новый regression test проверяет повреждение и удаление логов обоих статусов; после исправления он проходит.
- Дополнительно воспроизведено разрешение review автору hypothesis, если preregistration создал другой planner: contributors первоначально включали только protocol/claim/run authors. На момент этого отчёта исправление не проверено; статус этого пункта должен быть обновлён после отдельного regression test.

Строгие границы гарантий:

- Actor IDs и роли задаёт доверенный caller. Нет аутентификации, подписанных agent identities, изолированных credentials или доказательства независимости рассуждения. Два разных implementation hashes также не доказывают независимое происхождение кода. Нельзя предоставлять недоверенным агентам прямой `Store.append`, доступ к SQLite или общий writable filesystem и считать Python role checks security boundary.
- Demo использует фиксированные программы, свежие subprocess и Python `-I`. Это реальное offline вычисление, но не sandbox для произвольного враждебного кода. Network isolation, process quotas и filesystem isolation отсутствуют.
- Reanalysis работает с тем же raw dataset. Она проверяет согласие двух численных реализаций; это не независимая fresh-data replication, не проверка data leakage, не контроль systematic bias и не статистическая оценка надёжности результата.
- Synthetic generator намеренно создаёт положительный сигнал. Три seeds проверяют работу pipeline. Они не доказывают novel discovery, значимость эффекта, обобщаемость, causal validity или соответствие venue standard.
- Scope проверяется как точное равенство словарей. Соответствие текста claim данным, корректность prediction/falsifier, competing explanations, novelty и качество reviewer rationale не выводятся из JSON/hash checks.
- В API есть минимальные hypothesis/protocol/run/claim/review relations и protocol parent. Полноценные hypothesis tournament, experiment tree search, durable scheduler с recovery, автоматическое исполнение replanning, LLM agent adapters и автоматический литературный research не проверены как реализованные.
- Paper — внутренний evidence-linked scaffold. Проверены механический допуск, basis linkage и CAS provenance. Научная аргументация, literature synthesis, citations, figures, полнота methods, venue formatting и submission-level review остаются необходимой отдельной работой. Пользовательская цель целиком этим MVP не достигнута.
