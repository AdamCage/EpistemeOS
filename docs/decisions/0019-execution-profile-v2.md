# ADR 0019 — профиль исполнения v2: дерево исходников, закрытое окружение uv и воспроизведение

Дата: 4 октября 2026. Статус: **предложено; реализуется поэтапно в ветке `exec-profile-v2`**. Что реализовано и проверено, а что только запланировано, указано в разделе «Ход реализации»; числа проверок — в [validation.md](../validation.md). Принятие решения — при интеграции ветки в main. Основания: [архитектура](../architecture.md) §5, §9, §11; [MVP-план](../mvp-plan.md), M2 (provenance closure, матрица изоляции, чистый offline-запуск); ADR [0005](0005-local-runner.md), [0006](0006-execution-batches.md), [0016](0016-domain-pack-contract.md) (раздел «Место для execution profile и environment closure»); находка A-12 аудита `docs/adversarial-audit-2026-10-04.md` и понятие managed evidence из ADR 0018 `docs/decisions/0018-claim-families-and-review-admission.md`. Оба последних документа на момент написания есть только в рабочей копии main, поэтому здесь они указаны путями, а не ссылками.

## Проблема

Профиль [`trusted_local_python_v1`](0005-local-runner.md) исполняет ровно один stdlib-файл: `[sys.executable, "-I", "-S", "program.py", "input.dat", "--seed", N]`, без site-packages. Он записывает fingerprint интерпретатора и ОС (включая SHA-256 исполняемого файла Python), argv и SHA-256 программы и input. У этого профиля три ограничения.

- **Программы.** Многофайловую программу и сторонние зависимости (numpy, scipy, torch) исполнить нельзя. Fingerprint описывает интерпретатор, но не окружение, из которого программу можно воспроизвести на чистой машине. Сведений об ускорителях нет.
- **A-12.** Worker наследует всё окружение controller (`execution.py`, `Popen` без `env=`), так что программа читает переменные с секретами. Рабочий каталог `<root>/executions/<token>` лежит внутри корня хранилища, и `../../state.sqlite3` — это живой журнал, который затем оценивает результат этой программы.
- **Воспроизведение.** Команды `episteme reproduce` нет. Повторный запуск того же кода нельзя ни выполнить из CAS, ни отличить в истории от независимой проверки.

Docker на этом хосте недоступен ([validation](../validation.md), раздел о среде). Контейнеры остаются будущим isolation profile; этот ADR их не требует.

## Решение

Рядом с v1 вводится профиль **`uv_locked_python_v2`**. Профиль выбирает декларация окружения протокола: `schema_version=1, backend=trusted_local_python_v1` — это v1, `schema_version=2, profile=uv_locked_python_v2` — v2. Kernel не меняется. Поля `implementation`, `environment` и `data` протокола и run по-прежнему остаются digests; в v2 это digest манифеста дерева исходников, digest closure окружения и digest input. Типы событий `execution_job`, `execution_dispatch` и `execution_finalized` общие для обоих профилей. Профиль v2 отличается версией specification (`schema_version=2`), версией dispatch (`schema_version=2`, отдельная команда `execution.dispatch_v2`) и форматом completion (`schema_version=2`).

| | v1 `trusted_local_python_v1` | v2 `uv_locked_python_v2` |
|---|---|---|
| Программа | один файл `program.py` | дерево файлов в CAS с явной точкой входа |
| Окружение | fingerprint интерпретатора контроллера | closure: `pyproject.toml`, `uv.lock`, вложенные wheels, версия и реализация Python, платформа, версия uv, переменные |
| Установка | нет (stdlib, `-S`) | новое окружение на каждый job из lock, только offline |
| Переменные payload | всё окружение controller | только allowlist closure и управляемые профилем значения |
| Рабочий каталог | `<root>/executions/<token>` | `<execution root>/<token>` вне корня хранилища |
| Запись окружения | fingerprint в completion | установленные дистрибутивы с хешами, хеш интерпретатора, inventory, uv, GPU |
| Воспроизведение | только при совпадающем fingerprint | повторное создание окружения из CAS в новом каталоге |

Профиль v1 остаётся валидным и воспроизводимым при replay. Его код исполнения, рабочий каталог и наследование окружения сознательно не меняются: прежние specifications не содержат allowlist, и молчаливая смена условий исполнения изменила бы смысл уже записанных runs. Для новой работы предназначен v2.

### Программа: дерево исходников

`implementation` протокола v2 — SHA-256 canonical JSON манифеста:

```json
{"schema_version": 1, "kind": "source_tree", "entry_point": "main.py", "input_name": "input.dat",
 "files": [{"path": "helpers/stats.py", "sha256": "…", "bytes": 52}, {"path": "main.py", "sha256": "…", "bytes": 310}]}
```

Каждый файл лежит в CAS отдельно. Пути относительные, POSIX, отсортированы и уникальны. Компоненты переносимы (`[A-Za-z0-9_][A-Za-z0-9_.+-]*`, без зарезервированных имён Windows и завершающей точки), глубина не больше 16, путь не длиннее 1024 символов. Пути, совпадающие без учёта регистра, и одноимённые файл и каталог запрещены. Лимиты: 4096 файлов, 64 MiB на файл, 256 MiB на дерево. `entry_point` — один из `.py`-файлов дерева. `input_name` — имя файла, под которым input материализуется. Точка входа и имя input входят в identity программы: та же программа с другой точкой входа — это другая реализация, и для повторного анализа это допустимо.

`episteme execution source --tree DIR --entry main.py [--input-name NAME] --root R` замораживает каталог без событий. Скрытые имена (с точки) и `__pycache__` не захватываются. Ссылки и reparse points отвергаются, по ним не переходят. Захват каталога не делает его чистым checkout: замораживаются ровно те bytes, которые в нём есть.

### Окружение: closure и реализованное окружение

`environment` протокола v2 — SHA-256 canonical JSON closure:

```json
{"schema_version": 2, "profile": "uv_locked_python_v2",
 "install_policy": "uv_sync_frozen_offline_no_build_copy_v1",
 "project": [{"path": "pyproject.toml", "…": "…"}, {"path": "uv.lock", "…": "…"},
             {"path": "wheels/x-0.1.0-py3-none-any.whl", "…": "…"}],
 "python": {"implementation": "cpython", "version": "3.11.15"},
 "platform": {"system": "Windows", "machine": "AMD64"},
 "installer": {"tool": "uv", "version": "0.11.28"},
 "variables": {"inherit": ["NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"],
               "set": {"PYTHONHASHSEED": "0", "PYTHONUTF8": "1"}}}
```

**Identity окружения — digest closure.** Каждый run связан с ним через `run.environment` и через specification. Две closure с одинаковым lock, но разными версией Python, платформой, версией uv или переменными — разные окружения. В identity входят все bytes проекта, по которым uv создаёт окружение, точная версия и реализация интерпретатора, платформа (`platform.system()` и `machine()`), точная версия uv и рецепт установки. Бинарный файл интерпретатора закреплён не хешем, а версией. Его SHA-256 записывается на каждый run и сравнивается при воспроизведении. Закрепление хеша сделало бы воспроизведение на другой машине невозможным почти всегда, кроме одинаковых сборок python-build-standalone.

`uv.lock` проверяется до freeze. Принимаются корневой проект (`virtual` или `editable` с путём `.`) и дистрибутивы из registry. У удалённого registry каждый wheel и sdist обязан иметь `sha256`. Локальный registry должен быть относительным каталогом внутри проекта. Его wheels захватываются в closure как файлы проекта, потому что uv записывает для них только путь, без хеша: без захвата lock не закреплял бы их bytes. Абсолютные и выходящие наружу пути, локальные sdist, `git`, `path`, `directory`, `url` и editable-участники отвергаются: для них нужна сборка или они не переносимы. В closure ровно `pyproject.toml`, `uv.lock` и локальные wheels, на которые ссылается lock, — без лишних и пропущенных файлов. Согласованность `pyproject.toml` с lock не проверяется: `uv sync --frozen` ставит ровно lock, а проверка `uv lock --check` требовала бы резолвинга. Это ответственность автора closure.

`episteme execution closure --project DIR [--python X.Y.Z] [--implementation NAME] [--inherit NAME]... [--set NAME=VALUE]... --root R` замораживает closure без событий. По умолчанию берутся реализация и версия текущего интерпретатора, платформа этого хоста и версия uv, найденного на PATH или в `EPISTEME_UV`.

**Установка.** Каждый job получает новое окружение. Worker выполняет:

```text
uv python find <impl>@<version> --system --no-config --no-python-downloads --offline
uv sync --frozen --offline --no-config --no-install-project --no-build --no-python-downloads
        --link-mode copy --cache-dir <cache> --python <found> --project <job>/closure
```

с `UV_PROJECT_ENVIRONMENT=<job>/venv` и `cwd=<job>/tmp`.

- `--offline`: установка при job никогда не обращается к сети. Источник — локальный кэш uv и вложенные wheels.
- `--no-config`: пользовательская и системная конфигурация uv не влияет на установку; источники задаёт lock. Каталог кэша определяется по обычной конфигурации пользователя (`uv cache dir`), чтобы найти уже заполненный кэш.
- `--no-build`: при установке не исполняется код сборки; дистрибутивы, у которых нет wheel для платформы, приводят к отказу.
- `--link-mode copy`: программа, меняющая свои site-packages, не портит кэш uv. Жёсткие ссылки разделяли бы файлы с кэшем.
- `--system`: интерпретатор ищется среди установленных, а не в виртуальном окружении текущего каталога. Без этого флага uv в каталоге с `.venv` возвращает именно его.

Повторное использование готового venv между jobs отклонено. Программа под той же OS identity может изменить site-packages, а проверка полного inventory перед каждым запуском стоит примерно столько же, сколько копирование из кэша, и защищает хуже. Повторно используется только кэш wheels uv. Единственный сетевой путь — явная `episteme execution prepare <closure> --online --root R`. Она создаёт и проверяет окружение во временном каталоге, может заполнить кэш uv из сети и не пишет событий. Без `--online` та же команда проверяет, создаётся ли окружение офлайн.

**Запись реализованного окружения** (`environment.json` в CAS) фиксирует:

- digest closure и версию, путь и SHA-256 uv, каталог кэша, точную командную строку установки, её переменные и длительность;
- найденный интерпретатор: реализацию, версию, `sys.version`, executable venv, base executable и его SHA-256;
- платформу с точки зрения worker и вид, который видит payload;
- каждый установленный `*.dist-info`: имя, версию и installer, результат проверки всех хешей RECORD, число файлов и `content_sha256`. Этот digest исключает файлы, которые пишет installer (`INSTALLER`, `REQUESTED`, `RECORD`, `direct_url.json`, `uv_cache.json`), и всё, что RECORD кладёт в каталог скриптов venv: launchers console scripts и скрипты с переписанным shebang содержат путь интерпретатора venv. Поэтому один и тот же wheel даёт одно значение в любом каталоге. Каждый дистрибутив должен присутствовать в lock;
- полный inventory venv: каждый файл с SHA-256 и размером, ссылки без перехода по ним. `sha256` охватывает всё и проверяет неизменность внутри одного job. `portable_sha256` исключает activation scripts, `pyvenv.cfg`, файлы installer и те же сгенерированные скрипты: они содержат путь venv, а `uv_cache.json` различается между записями кэша.

Сведения об ускорителях (`accelerators` в completion): если `nvidia-smi` есть на PATH worker, записываются index, name, driver_version, memory.total и «CUDA Version» из заголовка. Иначе пишется `not_available` с причиной, а ошибка зонда записывается как `error`. Записывается наличие ускорителя, а не факт его использования программой.

Несовпадение версии uv, отсутствие интерпретатора нужной версии, другая платформа, промах офлайн-кэша, ошибка RECORD или дистрибутив вне lock дают **failed attempt до запуска payload**. В результате остаются причина и журнал установки. Это технический отказ, а не опровержение гипотезы, и слот attempt им расходуется, как в v1.

### Исполнение

**Каталог job вне корня хранилища.** Корень исполнения — `EPISTEME_EXECUTION_ROOT` или `<tempdir>/episteme-executions`. `execution.dispatch_v2(job, workspace_token, workspace_root)` при admission проверяет, что путь абсолютный и не совпадает с корнем хранилища и не лежит внутри него, и записывает корень в событие dispatch. Reconcile поэтому не зависит от переменных окружения последующего процесса. Replay проверяет только формат пути, чтобы восстановленные копии на другой ОС оставались читаемыми. Состав каталога:

```text
<root>/<token>/control/  spec.json identity.json closure.json program.json  (controller)
                         launch.lock started.json heartbeat.json completion.json
                         environment.json installer.log stdout.bin stderr.bin (worker)
               run/      src/<дерево>  inputs/<input_name>  <outputs>           (cwd payload)
               closure/  pyproject.toml uv.lock wheels/…
               venv/     tmp/  home/
```

После финализации venv удаляется. Остальные файлы остаются для осмотра; все нужные bytes уже в CAS. Каталоги jobs не входят в backup, как и в v1.

**Allowlist переменных.** Payload получает ровно:

- `inherit` — значения хоста для перечисленных имён (отсутствующие перечисляются в `inherited_missing`);
- `set` — фиксированные значения closure (по умолчанию `PYTHONHASHSEED=0`, `PYTHONUTF8=1`);
- управляемые профилем `PATH` (каталог `Scripts` или `bin` venv плюс системный каталог), `TEMP`, `TMP` или `TMPDIR` в `<job>/tmp`, `HOME` и `USERPROFILE` в `<job>/home`.

Closure не может объявить управляемые имена, `UV_*`, имена с `__`, а `PYTHON*` — только через `set` и только из короткого списка (`PYTHONHASHSEED`, `PYTHONUTF8`, `PYTHONIOENCODING`, `PYTHONWARNINGS`, `PYTHONFAULTHANDLER`). Имена, похожие на учётные данные (`TOKEN`, `SECRET`, `PASSW`, `API_KEY`, `ACCESS_KEY`, `AUTH`, …), отвергаются. Это эвристика против случайной записи секрета: значения переменных попадают в completion, то есть в CAS, backups и экспорты, и удалить их без нарушения хешей нельзя. Это не сканирование секретов. По умолчанию на Windows наследуются `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`, `SYSTEMDRIVE`, `SYSTEMROOT`, `WINDIR`: без них не загружаются некоторые DLL и пуст `platform.machine()`. На POSIX по умолчанию ничего не наследуется.

**Цепочка процессов.** Gate-процессом служит **базовый** интерпретатор с `-I -S`: маленький shim ждёт permit по stdin, затем запускает `venv python -s -B -c <bootstrap> src/<entry> inputs/<input> --seed N` и возвращает его код выхода. Bootstrap убирает `''` из `sys.path`, ставит каталог точки входа первым (как `python src/<entry>`) и выполняет её через `runpy`. Windows Job Object или POSIX process group устанавливаются до permit, как в v1. Причина цепочки: на Windows `Scripts\python.exe` venv — launcher, который запускает настоящий интерпретатор дочерним процессом. Если бы gate-процессом был launcher, его потомок мог бы появиться до назначения Job Object. `-I` в payload не используется: он игнорирует `PYTHONHASHSEED`. `-E` не нужен, потому что окружение и так целиком из allowlist; `-s` отключает user site, `-B` запрещает запись bytecode.

Наблюдение о v1 (не исправляется этим ADR): v1 запускает payload через `sys.executable` controller. Если это launcher venv на Windows, между созданием процесса и назначением Job Object существует то же окно гонки. На практике назначение выигрывает (тесты timeout v1 проходят), но гарантией это не является.

**Командная строка.** `run.command` v2 — логический argv `["python", "-s", "-B", "src/<entry>", "inputs/<input_name>", "--seed", "N"]` относительно каталога `run/`, где `python` — интерпретатор окружения job. Абсолютный путь интерпретатора нельзя знать при enqueue. Точная командная строка со всеми путями и кодом shim и bootstrap записывается в completion как `launch_command`, рядом с `working_directory` и точными `environment_variables`.

**Проверки во время исполнения.** До запуска и после выхода worker пересчитывает digest дерева `run/src` (лишний файл — изменение), проверяет, что `run/inputs` содержит ровно frozen input, и пересчитывает файлы closure. Любое изменение даёт `failed` («frozen inputs changed during execution»). Inventory venv после выхода сравнивается с записанным; отличие даёт `failed` («locked environment changed during execution»): программа, ставящая пакеты во время работы, не воспроизводима из lock. Outputs, лимиты capture, wall time и подтверждение остановки — как в v1. Установка ограничена 1800 секундами (`setup_seconds`, константа spec v2), payload — `wall_seconds`.

### Записи и проверки при переходах

| Переход | Проверка |
|---|---|
| `execution.enqueue` | Профиль по декларации окружения. Читаются манифест и все файлы дерева, closure и все файлы проекта, правила lock, input; outputs (правила v1 плюс зарезервированные `src`, `inputs` и controller labels `log`, `execution_manifest`, `environment_record`); лимиты; capabilities v2. Specification v2 связывает `program` = `run.implementation`, `environment` = `run.environment`, `command` = `run.command`, `input.sha256` = данные protocol или `raw_data` оригинала. |
| `execution.dispatch_v2` | Как v1 (назначенный actor, единственный dispatch, authority batch), плюс абсолютный корень вне хранилища. `execution.dispatch` отвергает v2 jobs, `execution.dispatch_v2` — v1 jobs. |
| `execution.finalize` | Completion v2: identity, command, статус, время, код выхода, подтверждённая остановка, честное поле `isolation`; для `completed` — равенство inputs до и после ожидаемым, запись окружения, согласованная с closure (digest, интерпретатор, платформа, версия uv), неизменный inventory, переменные только из allowlist с объявленными значениями `set`, `launch_command` с тем же хвостом argv, process control, все outputs и метрика protocol. |
| Replay (`_index`, Graph, gate, backup) | Те же связи specification и run на историческом префиксе. Все bytes дерева, closure и записи окружения читаются из CAS; повреждение даёт `IntegrityError`. Формат dispatch v2. Повторная проверка completion. Graph, инвентарь artifacts и backup охватывают файлы дерева, closure, запись окружения и журнал установки через `execution_artifacts`. |

Outputs результата v2: объявленные outputs, `log` и `execution_manifest` (completion), `environment_record`. Kernel и Graph используют их без изменений. У run v2 есть проверенный `execution_finalized`, поэтому по определению ADR 0018 его evidence — **managed**. Managed run по-прежнему не доказывает, что программа вычисляла, а не копировала: это проверяет пересчёт закреплённым доменным кодом.

Новые параметры в существующие команды не добавлялись. `CommandService` нормализует значения по умолчанию в fingerprint запроса, поэтому новый параметр сделал бы повтор старой receipt конфликтом. Отсюда отдельная команда `execution.dispatch_v2`.

### `episteme reproduce`

`episteme reproduce <run-id> --root R` повторно материализует программу, input и окружение из CAS и lock в новом каталоге вне хранилища, исполняет их тем же worker, сравнивает digests outputs и сообщает итог. Это **повтор того же кода в новом окружении** (`exact_rerun` в терминах архитектуры), а не независимая репликация. Code, data и closure те же; новой выборки, новой случайности или другой реализации нет.

Решение — **записывать каждую попытку**. Иначе расхождение можно было бы молча выбросить и повторять запуск, пока не совпадёт. Для этого вводятся два собственных типа событий:

- `execution_reproduction_dispatch` (`reproduction.dispatch`) — durable intent до запуска. Связывает run, job, finalization и result с их hashes, корень и token каталога, профиль и метку `replay`.
- `execution_reproduction` (`reproduction.finalize`) — проверенная completion воспроизведения, его outputs и запись окружения, детерминированное сравнение из CAS и вердикт `matched` или `mismatched`.

Попытка без finalize остаётся видимой как неизвестная. Новый `reproduce` создаёт новую попытку, а прежняя completion может быть сверена отдельно. Каждое событие помечено `counts_as_evidence=false`, `replication_mode=none` и `scientific_validity=not_assessed`. Это не `run` и не `result`: попытки не входят в claim evidence, roster batch, проверки gate на seeds и повторный анализ и в реестр попыток ADR 0018.

Вердикт `matched` означает тот же терминальный статус и побайтно равные объявленные outputs. Логи сравниваются информативно. Сравнение окружения (`distributions_sha256`, `portable_sha256`, SHA-256 интерпретатора, платформа) сообщается отдельно и вердикт не меняет. Отказ создать окружение (например, офлайн-кэш на другой машине пуст) — это `mismatched` с причиной. Это содержательный отрицательный результат о достаточности closure.

События воспроизведения **входят в evidence basis** claims, чьи runs они повторяют. Мнение reviewer, высказанное до попытки — совпавшей или нет, — становится несвежим. Расхождение нельзя скрыть от review, а совпадение не превращается в дополнительное подтверждение. Роли — только `executor` и `replicator`, по умолчанию actor и роль исходного job. Actor, записавший воспроизведение, становится contributor и не может рецензировать этот claim: роли и разные IDs не доказывают независимости.

Run v1 воспроизводится только интерпретатором, fingerprint которого совпадает с записанным, — тем же worker v1, но в новом каталоге вне хранилища. Окружение v1 не материализуемо, и метка это говорит (`same_code_same_interpreter`). Caller-recorded run без job, unknown attempt и не завершённый job отвергаются.

### DomainPack и batch

`compile_execution` может вернуть `ExecutionPlanV2` (schema `execution-plan-v2`): два дерева исходников (primary и reanalysis) как манифесты с bytes файлов, input, outputs, лимиты, capabilities v2, `execution_profile=uv_locked_python_v2` и `environment_requirements` с `closure_level=uv_lock`, файлами проекта closure (pyproject, lock, вложенные wheels) и обязательными значениями переменных. Пакет объявляет профиль в `execution_profiles` манифеста. Файлы пакета остаются `.py`, поэтому bytes проекта пакет держит константами. Ядро при `pack.preregister` проверяет, что переданная хостом closure содержит ровно эти файлы проекта и требуемые переменные. Версию Python, платформу и версию uv выбирает хост. `implementation` протокола — digest манифеста primary, `data` — input. Graph, backup и review exclusion охватывают все файлы деревьев и проекта. Существующие пакеты остаются на v1, и их bindings, компиляция и история не меняются.

`batch.plan` и `batch.enqueue_slot` принимают v2 recipe. `_recipe` проверяет каждую пару (implementation, environment) по её профилю. Смешанный batch (primary v2, повторный анализ v1 или наоборот) допустим. Capabilities batch должны поддерживаться профилем каждой пары.

## Честное заявление об изоляции

| Свойство | v2 |
|---|---|
| OS identity | Тот же пользователь, что controller. Отдельного пользователя, restricted token, AppContainer или namespace нет. |
| Файловая система | Не ограничена. Программа читает и пишет всё, что может этот пользователь, включая хранилище по абсолютному пути (это проверяет тест), CAS, кэш uv и каталог своего job. Переход `../../state.sqlite3` из рабочего каталога больше не ведёт в хранилище — это убирает только тривиальный вектор. |
| Сеть | Не ограничена и не наблюдается, ни на Windows, ни на POSIX. Офлайн только установка окружения. |
| Переменные окружения | Allowlist. Секреты controller, не перечисленные в closure, payload не получает. Это не мешает программе прочитать файлы с секретами. |
| Процессы | Job Object или process group, как в v1. Потомок, покинувший group на POSIX, не контролируется. Установщик uv ограничен таймаутом, его потомки не содержатся. |
| Целостность журнала | Не защищена. Программа может открыть SQLite по абсолютному пути и переписать историю (A-12, PoC B). Сверка head и `sqlite_master` до и после запуска, отдельный пользователь и read-only mounts остаются дальнейшей работой. |
| Кэш uv | Программа может испортить кэш тем же пользователем. `--link-mode copy` исключает только случайную порчу через жёсткие ссылки. |

Эти меры убирают тривиальные векторы A-12: наследуемые секреты и относительный путь к журналу. Песочницей они не являются. Программы остаются доверенным локальным кодом, как пакеты в ADR 0016.

## Совместимость

- Код и формат v1 не изменились, кроме ветвления по `schema_version` specification и отказа `execution.dispatch` для v2 jobs. Golden histories дают те же basis, gates, Graph, инвентарь и экспорт.
- Kernel (`_start_run`, `_finish_run`, gate) не менялся. Новые типы событий есть только у воспроизведения.
- События v2 читаются на любой ОС: replay не проверяет абсолютность путей хоста.

## CI и тесты

Unit tests не обращаются к сети. Программы — маленькие многофайловые stdlib-fixtures. Зависимость — минимальный валидный wheel, собранный в тесте через `zipfile` (METADATA, WHEEL, RECORD) и залоченный `uv lock --offline` из относительного find-links каталога. Тесты v2 требуют uv и пропускаются с явной причиной, если uv нет.

Решение для CI: workflow ставит закреплённый `uv==0.11.28` через pip, чтобы тесты v2 исполнялись во всех четырёх конфигурациях (Windows и Linux, Python 3.11 и 3.13). Интерпретатор closure в тестах — текущий (`platform.python_version()`); `uv python find --system` находит его на PATH runner. Изменение workflow локально не исполнялось: ветку не публикуют, поэтому CI внешне не проверен.

Необязательная локальная интеграционная проверка: closure с numpy из PyPI, `prepare --online` для заполнения кэша, затем офлайн job и воспроизведение. Ни один тест от неё не зависит.

## Стоимость и ресурсы

Учёт бюджета вне scope. Место оставлено так. Completion уже фиксирует измеримые величины: wall time payload, время установки, bytes outputs и inventory, наличие GPU. Запрос ресурсов (CPU, RAM, GPU, деньги) войдёт в specification следующей версии (`schema_version=3`), а расход — в отдельный ledger события, а не в completion. Cost unit batch остаётся `enqueued_attempt`.

## Отклонённые альтернативы

- **Сделать v1 безопаснее на месте** (allowlist и каталог вне хранилища для v1). Это меняет смысл записанных runs и reconcile уже dispatched v1 jobs, чей каталог лежит в хранилище.
- **Повторно использовать venv между jobs.** См. «Установка».
- **Устанавливать из сети по умолчанию или флагом job.** Сетевой путь отделён в `prepare --online`, без событий. Job либо создаёт окружение офлайн, либо честно падает.
- **Закрепить SHA-256 интерпретатора в closure.** Он записывается и сравнивается, но не закрепляется.
- **Не записывать воспроизведения или считать их replication.** Первое позволяет выбрасывать расхождения, второе смешивает same-code replay с независимой проверкой.
- **Исключить воспроизведения из basis.** Тогда мнение reviewer, высказанное до расхождения, оставалось бы свежим.
- **Docker/контейнер сейчас.** На хосте недоступен. Будущий профиль получит `image_digest` и собственную матрицу гарантий.

## Этапы реализации

1. Профиль v2 для отдельных jobs: форматы дерева и closure, worker, `execution.dispatch_v2`, completion и replay, CLI `execution source/closure/prepare`, тесты, CI.
2. `episteme reproduce`: события и команды воспроизведения, Graph, basis, v1 при совпадающем интерпретаторе, тесты.
3. Batch и DomainPack: `ExecutionPlanV2` и schema, `pack.preregister` и `batch.plan` для v2, тестовый пакет v2 через `pack.preregister` → batch → `pack.analyse`, обновление общих документов.

## Ход реализации

**Шаг 1 — реализован и проверен локально.** Модули `environment_closure.py` (форматы и правила, только stdlib), `runner_locked.py` (worker v2) и `execution_locked.py` (controller, CLI), ветвление профилей в `execution.py`, команда `execution.dispatch_v2`, подкоманды CLI `execution source`, `closure` и `prepare`, установка uv в CI. Тесты `tests/test_execution_locked.py`: правила манифестов, lock и переменных; многофайловая программа в новом окружении вне хранилища; payload не видит канарейку controller и не достигает журнала относительным путём, но достигает его абсолютным; offline-установка вложенного wheel с проверкой RECORD; промах офлайн-кэша, несовпадение uv или интерпретатора и изменения дерева, input или venv дают failed; повторный анализ вторым деревом проходит gate как managed evidence; профильные dispatch; unknown без повторного запуска; повреждённые bytes дерева ломают replay; Graph, экспорт и backup/restore; реальный CLI. Числа — в validation.md.

**Шаг 2 — реализован и проверен локально.** Модуль `reproduction.py`: события `execution_reproduction_dispatch` и `execution_reproduction`, команды `reproduction.dispatch` и `reproduction.finalize`, проверка при replay внутри общего `execution._index`, включение попыток в контекст исполнения claim (и тем самым в basis), artifacts в Graph и инвентаре, два новых типа узлов Graph и связь `execution_reproduced`, CLI `episteme reproduce <run> [--reconcile] [--actor] [--role]` с кодом 0 для `matched` и 1 иначе. Импорт completion v1 вынесен из `reconcile_job` в `_import_completion` без изменения поведения. Тесты `tests/test_reproduction.py`: совпадение v2 со всеми полями окружения, запись без новых run и result, Graph и backup/restore; недетерминированная программа даёт `mismatched` только по `raw_data`; попытка входит в basis claim, меняет его hash и не меняет claim evidence и contributors; отказ создать окружение — `mismatched` с причиной; неизвестная попытка видна, поздняя completion сверяется без повторного запуска, новый `reproduce` — новая попытка; v1 только при том же интерпретаторе, иначе отказ до записи; отказ для caller-recorded и незавершённого run; событие без receipt ломает Graph; коды выхода CLI. Необязательная проверка с numpy из PyPI описана в validation.md. Она выявила, что launchers console scripts содержат путь venv; после этого они исключены из `content_sha256` и `portable_sha256`, а fixture wheel получил console script.

Шаг 3 не реализован.

## Оставшаяся работа

- Сверка head и `sqlite_master` до и после запуска (A-12, PoC B), отдельная OS identity, ограничение сети и файловой системы, контейнерный профиль.
- Review assignment исключает из начального контекста reviewer только digests манифеста и closure, но не каждый файл дерева. Если программа копирует свой исходник в `raw_data`, сработает только проверка коллизии верхнего уровня.
- Протокол v2 без jobs ссылается в Graph только на digests манифеста и closure. Файлы раскрываются через `execution_job` и `pack_binding`; проверка closure при `kernel.preregister` не добавлена, отказ происходит при enqueue.
- Окно гонки launcher и Job Object в v1 на Windows.
- Удаление каталогов jobs, кроме venv, и квоты диска; рост кэша uv из-за записей локальных wheels на каждый job.
- Длинные пути Windows: для пакетов с глубокими путями корень исполнения должен быть коротким (`EPISTEME_EXECUTION_ROOT=C:\ex`).
