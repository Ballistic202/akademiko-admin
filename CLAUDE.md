# Академико Admin — Контекст за Claude Code

## Проект
RAG-базирана образователна система за Bulgarian K12. Admin app за качване и обработка на учебници.

## Стек
- **Backend**: Flask (Python) на Azure App Service
- **Storage**: Azure Blob Storage (`akademikostorage`) — контейнери: `textbooks-raw`, `chunks-pending`, `chunks-approved`, `ocr-confirmed`
- **Search**: Azure AI Search (`akademiko-search`) — индекс `akademiko-knowledge-source-index`
- **OCR**: Azure Document Intelligence (не-математически предмети) + Mathpix (Математика)
- **LLM**: Azure OpenAI — `gpt-4.1` (chunk-ване), `gpt-4o-mini` (Q&A и тагване), `text-embedding-3-small` (embeddings)

## Environment Variables (Azure App Service)
```
STORAGE_CONN, DOC_ENDPOINT, DOC_KEY, OPENAI_ENDPOINT, OPENAI_KEY,
SEARCH_ENDPOINT, SEARCH_KEY, MATHPIX_APP_ID, MATHPIX_APP_KEY
```

## Файлова структура
```
app.py                    — Flask backend
templates/index.html      — Frontend (single page app)
curriculum_complete.json  — Учебна програма МОН (класове 4-12, 598 раздела)
requirements.txt
```

## curriculum_complete.json структура
```json
{
  "10": {
    "География и икономика": {
      "sections": [
        {
          "name": "Географско положение и граници на България",
          "goals": ["Локализира...", "Характеризира..."],
          "key_concepts_mon": ["географско положение", "граници"]
        }
      ]
    }
  }
}
```

## Workflow за качване на учебници
1. **OCR стъпка** — качване на PNG снимки → Azure Doc Intelligence или Mathpix → OCR текст
   - Алтернативно: директно качване на готов .txt файл (нов feature)
2. **Редакция** — редакторът редактира текста, маха нерелевантното, слага `\n\n` между chunk-овете
3. **Chunk-ване** — механично разбиване по `\n\n` (НЕ GPT) → JSON масив от chunk-ове
4. **Тагване** — за всеки chunk поотделно: `/tag-chunk` endpoint → `gpt-4o-mini` определя `goals` и `key_concepts_mon` от curriculum
5. **Одобрение** → `chunks-approved` → индексер го взима

## Chunk JSON структура
```json
{
  "chunk_id": "гео-10-001",
  "subject": "География и икономика",
  "grade": 10,
  "section": "Географско положение и граници на България",
  "content_type": "main_text",
  "text_content": "текст на chunk-а",
  "key_concepts": [],
  "goals": ["Локализира природни области"],
  "key_concepts_mon": ["географско положение"]
}
```

## Azure AI Search индекс полета
`uid` (key), `snippet_parent_id`, `blob_url`, `snippet`, `snippet_vector` (1536d),
`section`, `goals` (Collection), `key_concepts_mon` (Collection), `text_content` (bg.microsoft),
`subject` (standard.lucene), `grade` (Edm.Int32), `content_type`

## Skillset — важно!
Полетата се попълват чрез `indexProjections.selectors.mappings` в skillset-а — НЕ чрез fieldMappings в indexer-а (при jsonArray parsingMode fieldMappings не работят за JSON съдържание).

## app.py — ключови endpoints
- `GET /curriculum` — връща curriculum_complete.json
- `POST /ocr-batch` — OCR на снимки
- `GET/POST /ocr-confirmed/<filename>` — четене/запис на OCR текст
- `POST /chunk-ai` — механично разбиване по `\n\n`, без GPT
- `POST /tag-chunk` — тагва един chunk с goals и key_concepts_mon (gpt-4o-mini)
- `POST /save-pending` — записва тагнатите chunk-ове в chunks-pending
- `POST /save-chunks` — мести от chunks-pending в chunks-approved
- `POST /qa` — vector search + GPT отговор с референции
- `GET /download-chunk/<path:filename>` — сваля chunk файл server-side (blob е private)
- `GET /stats` — статистика

## ТЕКУЩ ПРОБЛЕМ — /tag-chunk не слага метаданни
**Симптом**: След chunk-ването всички chunks имат `goals: []` и `key_concepts_mon: []`

**Диагноза**: Неизвестно дали:
1. `/tag-chunk` endpoint изобщо се извиква (проверете Log stream за `TAG-CHUNK:` записи)
2. Или се извиква но GPT връща празни масиви
3. Или `tagChunksProgress()` в index.html не се извиква поради JS грешка

**Как да дебъгнеш**:
- Добави в `/tag-chunk`: `app.logger.error(f"TAG-CHUNK: section={data.get('section')}, text_len={len(data.get('text',''))}")`
- Провери Browser Console (F12) за JS грешки по времето на chunk-ването
- Провери дали `tagChunksProgress()` се извиква след `/chunk-ai` response

**Логика на тагването в index.html**:
```javascript
// В startChunking() след fetch('/chunk-ai'):
const taggedChunks = await tagChunksProgress(data.chunks, data.filename);

// tagChunksProgress() итерира chunk по chunk:
for(let i = 0; i < total; i++) {
  const res = await fetch('/tag-chunk', { method: 'POST', ... });
  tagged[i].goals = data.goals || [];
  tagged[i].key_concepts_mon = data.key_concepts_mon || [];
}
// После извиква /save-pending с финалните chunk-ове
```

## Q&A логика
1. Embed въпроса с `text-embedding-3-small`
2. Vector search в Azure AI Search с филтър по `grade` и `subject`
3. Top 5 резултата → context за GPT
4. `gpt-4o-mini` генерира отговор
5. Ако отговорът съдържа "Нямам информация" → references: []
6. Иначе → references с filename за download

## Известни особености
- `parsingMode: jsonArray` в indexer-а — всеки елемент от JSON масива е отделен документ
- Blob storage е private — download минава през `/download-chunk/` server-side endpoint
- KaTeX в index.html за рендериране на LaTeX формули в Q&A отговорите
- Subject "Математика" → Mathpix OCR; всички останали → Doc Intelligence
