[https://github.com/AdamCage/llm-semantic-afterlife](https://github.com/AdamCage/llm-semantic-afterlife)

Изучи данный репозиторий и подход, который применяется для создания научных работ и проведения исследований с помощью агентов.

Наверняка существуют подобные и более успешные реализации подходов, которые позволяют с помощью агентской разработки проводить исследования и доводить их до серьезных публикаций и ценных научных работ.

Есть ли что-то такое?

Да. Причём за 2025–2026 годы появилось уже несколько систем, которые вышли далеко за пределы «агенты написали красивый PDF». Есть случаи реального peer review, экспериментальной валидации и публикаций в **Nature**.

После разбора `llm-semantic-afterlife` я бы сформулировал так: **ваш подход уже сильнее большинства существующих AI-research frameworks в части научной дисциплины и воспроизводимости, но слабее лучших систем в части поиска научного пространства — генерации альтернативных гипотез, ветвления экспериментов и накопления машинного “понимания” исследования.**

### Что именно у вас уже сделано правильно

`llm-semantic-afterlife` фактически реализует **scientific CI/CD**:

> plan → execute → artifacts → mechanical gate → scientific review → merge → re-plan

Причём эксперимент считается существующим только через `run_id`, конфиг, git SHA, seed и provenance; результаты должны быть воспроизводимы, артефакты содержат исходные tidy-data, отрицательные результаты должны сохраняться, а manuscript пишется только после завершения экспериментальных стадий.

Особенно удачно разделение **Executor / Supervisor**. Supervisor специально не проверяет то, что может проверить машина, а ищет перенос результатов между режимами, произвольные thresholds, confounds, необоснованные generalizations и смягчение отрицательных результатов.

Это не только декларация. Например, в PR #4 scientific review реально вернул `APPROVED WITH CHANGES` и заблокировал merge до исправления шести научно-методологических проблем.

То есть фундамент у вас хороший.

---

## Что сейчас является наиболее серьёзными аналогами

| Система                            | Что делает                                                             | Самый сильный внешний результат                                                        | Что вам оттуда нужно                 |
| ---------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------ |
| **Virtual Lab**                    | PI-agent + команда специализированных учёных + critic + human feedback | **Nature 2025**, реально разработаны и экспериментально проверены новые nanobodies     | Команда независимых scientific roles |
| **Robin**                          | literature → hypothesis → experiment → analysis → новая hypothesis     | **Nature 2026**, реальная biomedical discovery                                         | Замкнутый discovery loop             |
| **AI Scientist / AI Scientist-v2** | Idea → tree-search experiments → analysis → paper → review             | AI-paper прошёл peer review ICLR workshop; сама система опубликована в **Nature 2026** | Experiment tree search               |
| **Kosmos**                         | Long-horizon autonomous research + structured world model              | 7 discoveries, 4 заявлены как новые; независимая экспертная оценка                     | Research world model                 |
| **Google AI co-scientist**         | Generate / debate / rank / evolve hypotheses                           | Hypotheses получили wet-lab validation                                                 | Hypothesis tournament                |
| **Agent Laboratory**               | Literature → experiments → writing с human checkpoints                 | Findings of EMNLP 2025                                                                 | Практичный human/agent copilot       |
| **Denario**                        | Idea → novelty → method → computation → paper → review                 | Полностью сгенерированная работа принята Agents4Science 2025                           | Модульный paper pipeline             |

Но они существенно различаются по уровню доказательности.

---

# 1. Virtual Lab — один из самых важных примеров

Это, возможно, наиболее интересный для вас пример с точки зрения **организации научной команды агентов**.

Virtual Lab содержит LLM Principal Investigator и специализированных scientist-agents. Человек задаёт направление и даёт высокоуровневый feedback, после чего агенты проводят «research meetings». ([PubMed][1])

Они разработали computational pipeline на основе ESM, AlphaFold-Multimer и Rosetta и предложили **92 новых nanobody**. Затем живые исследователи синтезировали и проверили их — несколько действительно показали новые полезные свойства.

Работа опубликована в:

**Nature 646, 716–723 (2025)**. ([ДОИ][2])

То есть это уже не «AI написал статью про использование AI». Агентная система участвовала в получении нового экспериментального знания.

Архитектура тоже интересна:

**Human PI**
→ LLM PI
→ Immunologist
→ Computational biologist
→ ML specialist
→ Scientific critic
→ совместное обсуждение
→ решение.

Это важный контраст с вашей схемой **Executor → Reviewer**.

У вас separation of concerns хороший, но всего две основные интеллектуальные позиции.

Я бы обязательно украл отсюда **набор независимых scientific personas**, но не в смысле «поговорите между собой», а с разными формальными обязанностями.

[Virtual Lab repository](https://github.com/zou-group/virtual_lab?utm_source=chatgpt.com)

---

# 2. Robin — сейчас, вероятно, самый сильный результат

Здесь ситуация ещё интереснее.

Robin реализует настоящий итерационный цикл:

**literature search
→ hypothesis
→ proposed experiment
→ physical experiment
→ data analysis
→ revised hypothesis
→ next experiment**

В исследовании dry AMD Robin сначала предложил одну терапевтическую стратегию, после экспериментальных данных сам предложил RNA-seq, проанализировал его, выдвинул следующую гипотезу и в итоге вышел на ripasudil и KL001. ([FutureHouse][3])

FutureHouse утверждает, что **гипотезы, выбор экспериментов, анализ данных и основные figures** были сделаны Robin; люди выполняли физические эксперименты. ([FutureHouse][3])

Результат:

**Nature 655, 497–505 (2026)**. ([Nature][4])

Это очень серьёзный benchmark того, что вообще можно считать успешным agentic science.

Для Semantic Afterlife главный урок Robin не в multi-agent'ности.

Главный урок:

> **результат эксперимента должен менять следующий эксперимент.**

У вас это есть на уровне:

> stage → review → ADR → следующий stage.

Но Robin делает это гораздо более мелкозернисто внутри самого исследования.

Это одно из мест, где ваш framework можно сильно усилить.

[Robin repository](https://github.com/Future-House/robin?utm_source=chatgpt.com)

---

# 3. AI Scientist-v2 — наиболее прямой аналог именно вашего случая

Поскольку Semantic Afterlife — полностью computational research, именно Sakana AI Scientist является ближайшим аналогом.

Нынешняя версия использует **agentic tree search**.

Эксперимент — это не одна последовательность:

> придумал → реализовал → получил результат.

Создаётся дерево вариантов.

Каждый node содержит:

* experiment plan;
* код;
* execution result;
* metrics;
* critique кода;
* figures;
* VLM critique figures;
* статус.

После этапа evaluator выбирает лучшие branches, от которых начинается дальнейший поиск. ([Nature][5])

Причём экспериментирование разбито на:

1. initial investigation;
2. hyperparameter tuning;
3. research agenda;
4. ablations.

И увеличение числа experimental nodes статистически улучшает качество работы. ([Nature][5])

Вот **этого конкретно не хватает Semantic Afterlife**.

Сейчас у вас скорее:

```text
PLAN
   ↓
experiment
   ↓
finding
   ↓
review
   ↓
next PLAN
```

Я бы сделал:

```text
                ┌ experiment A1 ─ A2
hypothesis A ───┤
                └ experiment A3

                ┌ experiment B1
hypothesis B ───┤
                └ experiment B2 ─ B3
                     ↓
            scientific selection
                     ↓
             next experiment tree
```

При этом **ваш provenance/gate оставить полностью**.

### Но AI Scientist не стоит копировать целиком

Это важно.

Из трёх полностью AI-generated papers только **одна** прошла workshop review, получив 6/7/6. Ни одна, по оценке самих авторов, не достигла уровня main-track ICLR. ([Nature][5])

Nature прямо перечисляет проблемы:

* слабые идеи;
* неправильная реализация идей;
* недостаточная methodological rigor;
* ошибки экспериментов;
* hallucinated citations. ([Nature][5])

То есть по **scientific integrity ваш framework сейчас даже лучше защищён**.

Я бы взял у Sakana **tree search**, но не их trust model.

[AI Scientist-v2 repository](https://github.com/SakanaAI/AI-Scientist-v2?utm_source=chatgpt.com)

---

# 4. Kosmos — пожалуй, самое интересное направление для вашего следующего поколения

Здесь находится идея, которая мне кажется особенно подходящей Semantic Afterlife.

Kosmos отказался от надежды, что один длинный agent context сможет удерживать целое исследование.

Вместо этого существует **structured world model**.

Отдельные агенты занимаются literature search и data analysis, а результаты записываются в общую структурированную модель исследуемого мира. Благодаря этому Kosmos выдерживал около **200 rollouts**, выполнял в среднем ~42 000 строк кода и читал около 1 500 papers за один run. ([arXiv][6])

Причём statements в итоговом report привязываются либо к:

* primary literature;
* либо к выполненному code/evidence.

Независимые учёные оценили 79.4% утверждений в отчётах как корректные. Авторы приводят семь discoveries: три независимо воспроизвели результаты, которые Kosmos не видел, четыре рассматриваются ими как новые научные результаты. ([arXiv][6])

Это практически естественное продолжение вашей идеи с `runs + artifacts + ADR`.

Сейчас knowledge state у вас распределён между:

```text
research-plan.md
PLAN.md
REPORT.md
ADR-*.md
artifacts/
runs/
literature/
```

Человек или LLM должен каждый раз снова реконструировать состояние исследования.

Я бы добавил:

```text
research_state/
    hypotheses.jsonl
    claims.jsonl
    evidence.jsonl
    confounds.jsonl
    open_questions.jsonl
    literature_claims.jsonl
```

Например:

```text
CLAIM-041
"Qwen3-8B enters textual fixed points under P1 at T<=1.0"

SUPPORTED_BY:
 RUN-...
 RUN-...
 FIG-...

VALID_FOR:
 model=qwen3-8b
 protocol=P1
 W={4096,8192}
 T<=1.0

NOT_ESTABLISHED:
 other models
 true KV eviction

CONFOUNDS:
 repetition degeneracy

STATUS:
 supported
```

Это фактически **scientific knowledge graph вашего собственного исследования**.

Именно его должны читать агенты, а не каждый раз перерабатывать 50 Markdown-файлов.

---

# 5. Google AI co-scientist — очень хорош для стадии «что вообще проверить?»

Google использует специализированных агентов:

**Generation → Reflection → Ranking → Evolution → Proximity → Meta-review**. ([Google Research][7])

Главная идея — не принять первую нормальную гипотезу.

Создаётся множество гипотез, между ними происходят:

* scientific debates;
* pairwise comparisons;
* ranking tournaments;
* evolution/refinement.

Test-time compute буквально тратится на **поиск лучшей научной идеи**. ([Google Research][7])

И это не только synthetic benchmark: ряд biomedical hypotheses затем получил экспериментальное подтверждение, включая AML drug repurposing и liver fibrosis targets. ([arXiv][8])

Для вашего проекта это можно использовать перед новым stage.

Не:

> «Как проверить H5?»

А:

> Создай 20 competing explanations текущих результатов.

Например для вашей температурной картины:

* реальная dynamical phase transition;
* degeneration transition;
* entropy threshold phenomenon;
* sampling artefact;
* instruction-tuning attractor;
* effective-context phenomenon;
* tokenizer/block-size interaction;
* provider effect;
* etc.

Затем независимые agents должны предлагать **discriminating experiments**, которые максимизируют различие между competing explanations.

Это уже гораздо ближе к настоящей науке.

---

# 6. Agent Laboratory — хороший аргумент против полной автономности

Agent Laboratory структурирует процесс довольно традиционно:

**Literature Review → Experimentation → Report Writing**, с несколькими specialized agents. ([arXiv][9])

Но их особенно полезный результат другой.

Автоматический reviewer довольно сильно **переоценивал** AI-generated work: примерно **6.1/10 против 3.8/10 у human reviewers**. Кроме того, human feedback по ходу исследования заметно улучшал итоговое качество. ([arXiv][9])

Это подтверждает одно из ваших решений:

> **не давать executor'у самому себя окончательно сертифицировать.**

Ваш `stage-review` специально требует читать preregistered plan до REPORT и искать overclaiming, regime transfer, arbitrary thresholds и hidden confounds.

Это я бы не ослаблял.

---

# Что в итоге является best-of-breed архитектурой

Я бы не переходил ни на Agent Laboratory, ни на AI Scientist-v2 целиком.

Для `llm-semantic-afterlife` я бы построил **гибрид**:

```text
                         HUMAN PI
                            │
                            ▼
                    SCIENTIFIC MANAGER
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
      Literature Agent              Research State
      Novelty Agent                  / World Model
             │                             ▲
             ▼                             │
       Hypothesis Pool ────────────────────┘
             │
             ▼
      Debate / Tournament
             │
             ▼
       Experiment Manager
             │
       agentic tree search
       ┌─────┼─────┬─────┐
       ▼     ▼     ▼     ▼
     Exec   Exec  Exec   Exec
       │     │     │     │
       └─────┴──┬──┴─────┘
                ▼
        YOUR EXISTING SYSTEM
      runs / provenance / ledger
       artifacts / gate / CI
                │
                ▼
        Replication Agent
                │
                ▼
       Scientific Review Board
       ┌────────┼─────────┐
       ▼        ▼         ▼
    Methods   Stats     Novelty
    reviewer reviewer   reviewer
       └────────┬─────────┘
                ▼
           Meta-reviewer
                │
                ▼
             HUMAN
                │
                ▼
              MERGE
```

И только потом:

```text
approved claims
      +
evidence graph
      +
verified literature
      ↓
paper writer
      ↓
venue-specific reviewers
      ↓
manuscript
```

---

## Пять вещей, которые я бы добавил в ваш репозиторий в первую очередь

**1. Experiment tree.**
Это самый большой недостающий элемент. Не один executor-path на stage, а несколько competing experimental branches. Здесь разумно заимствовать AI Scientist/AIDE. AIDE уже показывает сильные результаты именно как tree-based ML experimentation scaffold. ([aide.ml][10])

**2. Machine-readable research world model.**
Kosmos-style claim → evidence → limitation → open question graph. Это резко уменьшит epistemic drift между агентными сессиями.

**3. Hypothesis tournament перед stage plan.**
Несколько моделей независимо предлагают competing explanations; reviewer-agent не просто оценивает их, а требует experiment, способный одну explanation отличить от другой.

**4. Три независимых scientific reviewers вместо одного.**
Например:

* methodology/statistics;
* domain/LLM dynamics;
* hostile TMLR/ICLR reviewer.

После них отдельный meta-reviewer. Причём желательно использовать **разные model families**, иначе ошибки будут коррелированы.

**5. Independent replication agent.**
После любого headline result агент получает только:

* frozen protocol;
* raw data либо API access;
* acceptance criteria.

И должен независимо реализовать analysis заново. Не читать исходную реализацию.

Это очень сильная защита от самого опасного класса ошибок: когда код, график и narrative согласованы друг с другом, но все три согласованно неправильны.

---

# Насколько далеко сейчас вообще можно зайти

На сентябрь 2026 года ответ уже не «это игрушки».

Есть как минимум два очень серьёзных доказательства:

**Virtual Lab → Nature 2025 → experimentally validated novel molecules.** ([ДОИ][2])

**Robin → Nature 2026 → iterative agent-driven biomedical discovery.** ([Nature][4])

Но универсальной системы вида:

> `research_question.md → нажал Enter → ICLR/TMLR/Nature`

по-прежнему нет.

Даже AI Scientist, являющийся самым прямым тестом такой идеи для ML, пока не показывает стабильного main-track уровня. Nature прямо пишет, что система **ещё не соответствует стабильно требованиям top-tier publication**. ([Nature][5])

И PaperBench хорошо показывает фундаментальную причину: даже **воспроизвести уже известную top-tier работу** агентам трудно; в первоначальной оценке лучший агент получал лишь 21% среднего replication score на 20 ICML Spotlight/Oral papers. ([OpenAI][11])

Поэтому я бы не стремился сделать Semantic Afterlife **более автономным**.

Я бы стремился сделать его **более параллельным, более состязательным и более epistemically structured**.

Ваши `preregistration + provenance + gates + artifacts + negative-results + scientific supervisor` — я бы оставил почти без изменений.

А сверху добавил бы три вещи из наиболее успешных систем:

**AI Scientist:** tree search по экспериментам.
**Google co-scientist:** tournament по гипотезам.
**Kosmos:** persistent world model / claim-evidence graph.

А модель организации научной команды взял бы из **Virtual Lab / Robin**.

Получится, на мой взгляд, архитектура заметно сильнее любого из этих решений по отдельности именно для вычислительного ML/LLM-исследования, где все эксперименты могут выполняться агентами без физической лаборатории.

[1]: https://pubmed.ncbi.nlm.nih.gov/40730228/?utm_source=chatgpt.com "The Virtual Lab of AI agents designs new SARS-CoV-2 nanobodies - PubMed"
[2]: https://doi.org/10.1038/s41586-025-09442-9?utm_source=chatgpt.com "The Virtual Lab of AI agents designs new SARS-CoV-2 nanobodies | Nature"
[3]: https://www.futurehouse.org/research/demonstrating-end-to-end-scientific-discovery-with-robin-a-multi-agent-system "Demonstrating end-to-end scientific discovery with Robin | FutureHouse"
[4]: https://www.nature.com/articles/s41586-026-10652-y?utm_source=chatgpt.com "A multi-agent system for automating scientific discovery | Nature"
[5]: https://www.nature.com/articles/s41586-026-10265-5 "Towards end-to-end automation of AI research | Nature"
[6]: https://arxiv.org/abs/2511.02824 "Kosmos: An AI Scientist for Autonomous Discovery"
[7]: https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/ "Accelerating scientific breakthroughs with an AI co-scientist"
[8]: https://arxiv.org/abs/2502.18864?utm_source=chatgpt.com "Towards an AI co-scientist"
[9]: https://arxiv.org/abs/2501.04227 "Agent Laboratory: Using LLM Agents as Research Assistants"
[10]: https://www.aide.ml/?utm_source=chatgpt.com "Home"
[11]: https://openai.com/index/paperbench/?utm_source=chatgpt.com "PaperBench: Evaluating AI’s Ability to Replicate AI Research | OpenAI"
