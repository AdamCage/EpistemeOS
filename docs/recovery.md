# Переносимый snapshot состояния и восстановление

Контракт локального M1 от 16 сентября 2026. Реализация: [recovery.py](../src/episteme/recovery.py); основания: [архитектура](architecture.md), [MVP-план](mvp-plan.md), [command receipts](decisions/0001-command-admission.md). Результаты проверок ведутся в [validation.md](validation.md).

Snapshot переносит SQLite-состояние вместе с content-addressed artifacts в новый каталог. Он сохраняет научную историю и историю доставки команд, включая неудачные попытки, отрицательные результаты и прежние версии решений. Backup и restore не выполняют эксперименты и не создают научных approvals.

С local runner от 18 сентября завершённые specification/completion/log/output artifacts входят в CAS и проверку Graph. Активные каталоги `executions/` не входят в snapshot. Восстановленный dispatched job без finalized остаётся `unknown`; restore не даёт новое право запуска. Snapshot очереди, сделанный до dispatch, и исходный store не разделяют глобальный execution lock: writable clones нельзя считать одной очередью с общей гарантией однократного исполнения. Автоматического resume после restore нет; [ADR 0005](decisions/0005-local-runner.md) описывает границы.

## CLI и API

С [batch layer от 21 сентября](decisions/0006-execution-batches.md) planner связывает набор запусков с hash локального `.execution-authority.json`. Marker намеренно не включён в snapshot. Восстановленный batch можно читать и экспортировать, но новые slot admissions и dispatch через `batch advance` или `execution work` отклоняются без соответствующего marker. Автоматического handoff нет; копирование token вручную не создаёт распределённой гарантии единственного исполнителя.

```powershell
uv run episteme backup --root .research/study --output .research/study-snapshot
uv run episteme restore .research/study-snapshot --root .research/study-restored
```

В первом вызове `--root` указывает на существующий store, `--output` — на новый каталог snapshot. Во втором позиционный аргумент указывает на snapshot, `--root` — на новый store. Родитель целевого каталога должен существовать. Целевой каталог не должен существовать даже пустым; broken symlink также считается занятым путём. Исходный и целевой каталоги не могут совпадать или содержать друг друга.

Обе команды возвращают JSON manifest. Python API: `backup(store, destination)` и `restore(snapshot, destination)` из `episteme.recovery`. Backup требует connection вне любой активной транзакции. CLI открывает исходный store read-only. Restore не переиспользует существующий store и не сливает две истории.

## Формат directory snapshot v1

```text
study-snapshot/
  manifest.json
  state.sqlite3
  artifacts/
    sha256/
      <64 lowercase hex digest>
```

`manifest.json` — canonical JSON с точным набором полей:

| Поле | Значение |
|---|---|
| `format`, `version` | `episteme-state-directory`, `1` |
| `created_at` | Время создания snapshot |
| `database` | SHA-256 и размер standalone `state.sqlite3` |
| `artifacts` | Отображение digest каждого включённого artifact в размер bytes |
| `revision`, `snapshot_hash` | Число событий и head hash проверенной event chain |
| `receipt_count`, `receipts_hash` | Число receipts и SHA-256 их canonical проверенного списка |

Snapshot не содержит ZIP/TAR, WAL/SHM, секретов из окружения процесса или произвольных файлов рабочей директории. CAS переносится целиком в пределах наблюдавшегося набора digest-файлов; содержимое уже сохранённых artifacts сохраняется без фильтрации. Manifest не допускает путей вместо digest, повторных JSON keys или неизвестных полей/версии. Размер manifest ограничен 64 MiB.

Restore требует точного совпадения directory inventory с manifest: дополнительных корневых файлов, CAS-файлов и подкаталогов быть не должно. Переносимые файлы и проверяемые каталоги должны быть обычными filesystem entries; symbolic links и Windows reparse points в этой структуре отвергаются.

## Согласованность и проверка

Backup сначала создаёт копию через SQLite online backup API, переводит её в `DELETE` journal mode и закрывает connection. Копия не зависит от исходных `-wal`/`-shm`. События и receipts входят в одну согласованную ревизию БД; два отдельно снятых JSONL exports такой гарантии не дают.

После копирования БД backup перечисляет CAS. Он переносит все наблюдавшиеся файлы с именем SHA-256, включая orphans, и проверяет digest bytes каждого файла. Имена временных файлов `Store.put()` не являются digest и игнорируются. Конкурентный writer может добавить artifacts после snapshot БД; они допустимы в копии как superset. Это согласованный snapshot БД с полным наблюдавшимся CAS, а не одновременный snapshot всего filesystem. Гарантия опирается на текущую append-only модель CAS без конкурентного garbage collection.

Перед публикацией staging проходит `PRAGMA integrity_check`, проверку event chain, проверку receipts и построение `ResearchGraph`. Graph проверяет ссылки и bytes всех доступных ему referenced artifacts, включая вложенные historical snapshots. Это обнаруживает отсутствующий referenced blob, которого уже нет в перечисляемом CAS. Поддерживаются текущие известные event kinds; неизвестный kind блокирует эту проверку вместо молчаливого пропуска его возможных зависимостей.

Restore копирует данные в собственный staging, сверяет размеры и SHA-256 с manifest и выполняет те же проверки. Затем он сравнивает revision, head hash и receipt identity с manifest. Проверка целостности не означает mechanical gate pass конкретного claim или scientific validity.

## Сохранение событий и идемпотентности

Restore переносит БД, не воспроизводя команды через Kernel. Сохраняются исходные event IDs, sequence, timestamps, payloads и hashes; receipt IDs, request/context fingerprints, event ranges и результаты команд также остаются исходными. Научные ссылки, старые reviews и supersession history не пересоздаются.

Повтор принятой команды с тем же полным envelope, включая исходный `expected_revision`, возвращает сохранённый result без нового события, даже если после неё были другие события. Повтор command ID с изменённым context/request остаётся conflict. Полученный исторический result не отменяет проверку актуальности evidence при новом переходе или materialization paper.

У старой БД может отсутствовать receipt table. Snapshot сохраняет эту историю как есть; receipts для legacy events не выдумываются. Последующее writable открытие Store применяет существующее аддитивное создание таблицы. Это не общий механизм миграции произвольных схем.

## Публикация и прерывание операции

Проверка выполняется в собственном временном каталоге рядом с назначением. Только после её завершения `mkdir(exist_ok=False)` эксклюзивно резервирует новый destination; существующий каталог не заменяется.

Далее artifacts переносятся первыми. Для restore `state.sqlite3` устанавливается последним; для backup после БД последним устанавливается `manifest.json`. Потребитель открывает восстановленный Store после успешного завершения restore. Обычное writable открытие Store умеет создавать БД и поэтому не служит способом ожидания готовности незавершённого destination.

Публикация не атомарна относительно видимости всего каталога. Ошибка или завершение процесса после reservation могут оставить неполный занятый destination. Повтор использует новый путь; backup/restore не накладывает файлы поверх неполного результата и не объявляет его успешным. До reservation ошибка проверки не создаёт destination. Очистка staging относится только к временному каталогу, созданному самой операцией.

## Границы контракта

Работа выполняется доверенным локальным caller на доверенном filesystem. Проверки entries и no-overwrite не являются OS sandbox или защитой от владельца файлов, который одновременно подменяет пути и данные. Manifest и hashes выявляют несогласованность и порчу; они не предоставляют внешнюю аутентификацию или независимый checkpoint истории.

Snapshot сохраняет имеющиеся artifacts, но не восстанавливает окружение исполнения, внешние datasets/services, активные процессы или состояние удалённых API. Он не перезапускает прерванные runs. Полное environment reconstruction, leases/outbox и reconciliation фактического исполнения остаются в M2; общий migration framework остаётся отдельной задачей M1.
