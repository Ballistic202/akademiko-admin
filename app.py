from flask import Flask, jsonify, request, render_template, Response
from azure.storage.blob import BlobServiceClient
import os
import json
import requests as req
import time
import base64
from urllib.parse import unquote

app = Flask(__name__)

blob_client = BlobServiceClient.from_connection_string(
    os.environ["STORAGE_CONN"]
)

def load_curriculum():
    try:
        path = os.path.join(os.path.dirname(__file__), "curriculum_complete.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"Could not load curriculum: {e}")
        return {}

CURRICULUM = load_curriculum()

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/curriculum", methods=["GET"])
def get_curriculum():
    return jsonify(CURRICULUM)

# ─── DOWNLOAD CHUNK ───────────────────────────────────────────────────────────

@app.route("/download-chunk/<path:filename>", methods=["GET"])
def download_chunk(filename):
    try:
        filename = unquote(filename)
        blob = blob_client.get_blob_client("chunks-approved", filename)
        content = blob.download_blob().readall()
        return Response(
            response=content,
            status=200,
            mimetype='application/json',
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        app.logger.error(f"Download error: {e}")
        return jsonify({"error": str(e)}), 404

# ─── PENDING / APPROVE / REJECT ───────────────────────────────────────────────

@app.route("/pending", methods=["GET"])
def list_pending():
    container = blob_client.get_container_client("chunks-pending")
    blobs = [b.name for b in container.list_blobs()]
    return jsonify({"pending": blobs})

@app.route("/chunks/<filename>", methods=["GET"])
def get_chunks(filename):
    blob = blob_client.get_blob_client("chunks-pending", filename)
    content = blob.download_blob().readall()
    return jsonify(json.loads(content))

@app.route("/approve/<filename>", methods=["POST"])
def approve(filename):
    source = blob_client.get_blob_client("chunks-pending", filename)
    dest = blob_client.get_blob_client("chunks-approved", filename)
    dest.start_copy_from_url(source.url)
    source.delete_blob()
    return jsonify({"status": "approved", "file": filename})

@app.route("/reject/<filename>", methods=["POST"])
def reject(filename):
    blob = blob_client.get_blob_client("chunks-pending", filename)
    blob.delete_blob()
    return jsonify({"status": "rejected", "file": filename})

# ─── OCR HELPERS ──────────────────────────────────────────────────────────────

def ocr_with_mathpix(file_bytes, filename):
    app_id = os.environ.get("MATHPIX_APP_ID", "")
    app_key = os.environ.get("MATHPIX_APP_KEY", "")

    if filename.lower().endswith(".png"):
        mime_type = "image/png"
    elif filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
        mime_type = "image/jpeg"
    else:
        mime_type = "image/png"

    image_b64 = base64.b64encode(file_bytes).decode("utf-8")

    res = req.post(
        "https://api.mathpix.com/v3/text",
        headers={
            "app_id": app_id,
            "app_key": app_key,
            "Content-Type": "application/json"
        },
        json={
            "src": f"data:{mime_type};base64,{image_b64}",
            "formats": ["text"],
            "data_options": {
                "include_latex": True
            },
            "options": {
                "math_inline_delimiters": ["$", "$"],
                "math_display_delimiters": ["$$", "$$"],
                "rm_spaces": True
            }
        },
        timeout=60
    )

    result = res.json()
    if "text" in result:
        return result["text"]
    else:
        app.logger.error(f"Mathpix error: {result}")
        return ""

def ocr_with_doc_intelligence(file_bytes, filename, endpoint, key):
    if filename.lower().endswith(".png"):
        content_type = "image/png"
    elif filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
        content_type = "image/jpeg"
    else:
        content_type = "application/pdf"

    url = f"{endpoint}documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30&locale=bg"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": content_type}
    response = req.post(url, headers=headers, data=file_bytes)

    if "Operation-Location" not in response.headers:
        return ""

    operation_url = response.headers["Operation-Location"]
    while True:
        result = req.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}).json()
        if result["status"] == "succeeded":
            break
        if result["status"] == "failed":
            return ""
        time.sleep(2)

    return result.get("analyzeResult", {}).get("content", "")

# ─── СТЪПКА 1: OCR ────────────────────────────────────────────────────────────

MATH_SUBJECTS = ["Математика"]

@app.route("/ocr-batch", methods=["POST"])
def ocr_batch():
    files = request.files.getlist("files")
    subject = request.form.get("subject", "")
    grade = request.form.get("grade", "")
    publisher = request.form.get("publisher", "")
    section = request.form.get("section", "")
    session_id = request.form.get("session_id", "")

    if not files:
        return jsonify({"status": "error", "message": "Няма файлове"}), 400

    endpoint = os.environ["DOC_ENDPOINT"]
    key = os.environ["DOC_KEY"]

    use_mathpix = subject in MATH_SUBJECTS
    app.logger.error(f"OCR mode: {'Mathpix' if use_mathpix else 'Doc Intelligence'} for subject: {subject}")

    all_text = []

    for file in files:
        filename = file.filename
        file_bytes = file.read()

        upload_blob = blob_client.get_blob_client("textbooks-raw", filename)
        upload_blob.upload_blob(file_bytes, overwrite=True)

        if use_mathpix:
            page_text = ocr_with_mathpix(file_bytes, filename)
        else:
            page_text = ocr_with_doc_intelligence(file_bytes, filename, endpoint, key)

        if page_text:
            all_text.append(f"=== {filename} ===\n{page_text}")

    combined_text = "\n\n".join(all_text)
    txt_filename = f"{session_id}.txt"
    txt_blob = blob_client.get_blob_client("ocr-confirmed", txt_filename)
    txt_blob.upload_blob(combined_text.encode("utf-8"), overwrite=True)

    return jsonify({
        "status": "ok",
        "session_id": session_id,
        "filename": txt_filename,
        "text": combined_text,
        "subject": subject,
        "grade": grade,
        "publisher": publisher,
        "section": section
    })


@app.route("/ocr-confirmed/<filename>", methods=["GET"])
def get_ocr_text(filename):
    blob = blob_client.get_blob_client("ocr-confirmed", filename)
    content = blob.download_blob().readall()
    return jsonify({"text": content.decode("utf-8")})


@app.route("/ocr-confirmed/<filename>", methods=["POST"])
def save_ocr_text(filename):
    data = request.json
    text = data.get("text", "")
    blob = blob_client.get_blob_client("ocr-confirmed", filename)
    blob.upload_blob(text.encode("utf-8"), overwrite=True)
    return jsonify({"status": "ok", "filename": filename})

# ─── СТЪПКА 2: AI CHUNKING ────────────────────────────────────────────────────

def get_section_data(grade, subject, section):
    try:
        grade_data = CURRICULUM.get(str(grade), {})
        subject_data = grade_data.get(subject, {})
        sections = subject_data.get("sections", [])
        for sec in sections:
            if sec.get("name") == section:
                return sec.get("goals", []), sec.get("key_concepts_mon", [])
    except Exception:
        pass
    return [], []

def call_gpt_chunk(chat_url, openai_key, system_prompt, user_prompt):
    """Извиква GPT и връща chunk-ове с partial parse fallback"""
    chat_res = req.post(chat_url,
        headers={"api-key": openai_key, "Content-Type": "application/json"},
        json={
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 32000,
            "temperature": 0.1
        },
        timeout=180
    )

    app.logger.error(f"GPT status: {chat_res.status_code}")

    try:
        response_json = chat_res.json()
        finish_reason = response_json["choices"][0]["finish_reason"]
        app.logger.error(f"GPT finish_reason: {finish_reason}")
        raw = response_json["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
    except Exception as e:
        app.logger.error(f"GPT response parse error: {e} | raw: {chat_res.text[:300]}")
        return []

    # Опит за пълен parse
    try:
        return json.loads(raw)
    except Exception as e:
        app.logger.error(f"Full JSON parse failed: {e} | raw length: {len(raw)}")

    # Fallback: partial parse — намери последния валиден chunk
    try:
        last_brace = raw.rfind('},')
        if last_brace > 0:
            partial = raw[:last_brace+1] + ']'
            chunks = json.loads(partial)
            app.logger.error(f"Partial parse recovered {len(chunks)} chunks")
            return chunks
    except Exception as e2:
        app.logger.error(f"Partial parse also failed: {e2}")

    return []

@app.route("/chunk-ai", methods=["POST"])
def chunk_ai():
    data = request.json
    text = data.get("text", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    section = data.get("section", "")
    session_id = data.get("session_id", "")

    openai_endpoint = os.environ["OPENAI_ENDPOINT"]
    openai_key = os.environ["OPENAI_KEY"]

    goals, key_concepts_mon = get_section_data(grade, subject, section)

    mon_context = ""
    if goals or key_concepts_mon:
        goals_text = "\n".join(f"- {g}" for g in goals) if goals else "Няма"
        concepts_text = ", ".join(key_concepts_mon) if key_concepts_mon else "Няма"
        mon_context = f"""
Учебна програма на МОН за раздел '{section}':

Компетентности (цели):
{goals_text}

Ключови понятия:
{concepts_text}
"""

    is_math = subject in MATH_SUBJECTS

    if is_math:
        system_prompt = """Ти си експерт по образователно съдържание. Анализирай предоставения математически учебен текст и го раздели на смислени chunk-ове подходящи за RAG система.

Текстът съдържа LaTeX формули вградени като $формула$ (inline) и $$формула$$ (display). Запази ги непроменени в text_content.

За всеки chunk създай JSON обект със следните полета:
- chunk_id: уникален идентификатор (напр. "mat-5-001")
- subject: предмет
- grade: клас като число
- section: раздел от учебната програма
- content_type: "main_text" за теория с формули, "definition" за дефиниции, "theorem" за теореми и правила, "example" за примери с решения, "exercise" за задачи
- text_content: текстът на chunk-а с LaTeX формулите (макс 800 символа)
- key_concepts: масив с ключови математически понятия от chunk-а
- goals: масив с цели от МОН програмата които покрива този chunk
- key_concepts_mon: масив с ключови понятия от МОН програмата в този chunk

Правила:
- Семантично завършен и самостоятелен chunk
- Минимум 50, максимум 800 символа
- Не разделяй формули в средата
- Дефиниции и теореми → отделни chunk-ове
- Чисти задачи без теория → content_type "exercise"
- Запази LaTeX формулите точно както са

Върни САМО валиден JSON масив без никакъв друг текст."""
    else:
        system_prompt = """Ти си експерт по образователно съдържание. Анализирай предоставения учебен текст и го раздели на смислени chunk-ове подходящи за RAG система.

За всеки chunk създай JSON обект със следните полета:
- chunk_id: уникален идентификатор (напр. "предмет-клас-номер")
- subject: предмет
- grade: клас като число
- section: раздел от учебната програма
- content_type: тип съдържание (main_text, glossary, questions, exercise, table, example)
- text_content: текстът на chunk-а (макс 800 символа)
- key_concepts: масив с ключови понятия от текста
- goals: масив с цели от МОН програмата които покрива този chunk
- key_concepts_mon: масив с ключови понятия от МОН програмата в този chunk

Правила:
- Семантично завършен и самостоятелен chunk
- Минимум 50, максимум 800 символа
- Не разделяй изречения
- Речникови дефиниции → отделен chunk
- Въпроси и задачи → отделен chunk

Върни САМО валиден JSON масив без никакъв друг текст."""

    # Разбий на части ако текстът е голям
    text_limit = 15000
    if len(text) > text_limit:
        parts = []
        for i in range(0, len(text), text_limit):
            parts.append(text[i:i+text_limit])
        app.logger.error(f"Text split into {len(parts)} parts (total {len(text)} chars)")
    else:
        parts = [text]

    chat_url = f"{openai_endpoint}openai/deployments/gpt-4.1/chat/completions?api-version=2024-02-01"
    app.logger.error(f"CHUNK-AI: grade={grade}, subject={subject}, section={section}, text_len={len(text)}, parts={len(parts)}, goals={len(goals)}")

    all_chunks = []
    for part_idx, part in enumerate(parts):
        user_prompt = f"""Предмет: {subject}
Клас: {grade}
Раздел: {section}
{mon_context}
Текст за chunk-ване{f' (част {part_idx+1} от {len(parts)})' if len(parts) > 1 else ''}:
{part}"""

        chunks_part = call_gpt_chunk(chat_url, openai_key, system_prompt, user_prompt)
        app.logger.error(f"Part {part_idx+1}: {len(chunks_part)} chunks")

        # Преномерирай chunk_id за да няма дубликати между частите
        if part_idx > 0:
            offset = len(all_chunks)
            for c in chunks_part:
                if "chunk_id" in c:
                    try:
                        parts_id = c["chunk_id"].rsplit("-", 1)
                        if len(parts_id) == 2 and parts_id[1].isdigit():
                            c["chunk_id"] = f"{parts_id[0]}-{int(parts_id[1]) + offset:03d}"
                    except Exception:
                        pass

        all_chunks.extend(chunks_part)

        if len(parts) > 1:
            time.sleep(1)

    app.logger.error(f"Total chunks: {len(all_chunks)}")

    output_name = f"{session_id}_chunks.json"
    dest = blob_client.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(all_chunks, ensure_ascii=False, indent=2), overwrite=True)

    return jsonify({
        "status": "ok",
        "chunks": all_chunks,
        "filename": output_name
    })


@app.route("/save-chunks", methods=["POST"])
def save_chunks():
    data = request.json
    filename = data.get("filename", "")
    chunks = data.get("chunks", [])

    dest = blob_client.get_blob_client("chunks-approved", filename)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)

    pending = blob_client.get_blob_client("chunks-pending", filename)
    try:
        pending.delete_blob()
    except Exception:
        pass

    return jsonify({"status": "ok", "filename": filename, "count": len(chunks)})

# ─── Q&A ──────────────────────────────────────────────────────────────────────

@app.route("/qa", methods=["POST"])
def qa():
    data = request.json
    question = data.get("question", "")
    grade = data.get("grade")
    subject = data.get("subject", "")

    search_endpoint = os.environ["SEARCH_ENDPOINT"]
    search_key = os.environ["SEARCH_KEY"]
    openai_endpoint = os.environ["OPENAI_ENDPOINT"]
    openai_key = os.environ["OPENAI_KEY"]

    embed_url = f"{openai_endpoint}openai/deployments/text-embedding-3-small/embeddings?api-version=2024-02-01"
    embed_res = req.post(embed_url,
        headers={"api-key": openai_key, "Content-Type": "application/json"},
        json={"input": question}
    )
    vector = embed_res.json()["data"][0]["embedding"]

    filters = []
    if grade:
        try:
            filters.append(f"grade eq {int(grade)}")
        except Exception:
            pass
    if subject:
        safe_subject = subject.replace("'", "''")
        filters.append(f"subject eq '{safe_subject}'")
    filter_str = " and ".join(filters) if filters else None

    search_url = f"{search_endpoint}indexes/akademiko-knowledge-source-index/docs/search?api-version=2024-07-01"
    search_body = {
        "search": question,
        "vectorQueries": [{"kind": "vector", "vector": vector, "fields": "snippet_vector", "k": 10}],
        "select": "text_content,snippet,blob_url,section,subject,grade",
        "top": 5
    }
    if filter_str:
        search_body["filter"] = filter_str

    search_res = req.post(search_url,
        headers={"api-key": search_key, "Content-Type": "application/json"},
        json=search_body
    )

    search_response_json = search_res.json()
    app.logger.error(f"Search filter: {filter_str}")
    app.logger.error(f"Search results count: {len(search_response_json.get('value', []))}")
    results = search_response_json.get("value", [])

    context = "\n\n".join([
        r.get("text_content") or r.get("snippet") or ""
        for r in results
        if r.get("text_content") or r.get("snippet")
    ])

    if not context.strip():
        return jsonify({
            "answer": "Нямам информация по този въпрос в наличните учебни материали.",
            "image_url": None,
            "references": []
        })

    chat_url = f"{openai_endpoint}openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-02-01"
    chat_res = req.post(chat_url,
        headers={"api-key": openai_key, "Content-Type": "application/json"},
        json={
            "messages": [
                {"role": "system", "content": "Отговаряй на български на база предоставеното учебно съдържание. Ако контекстът съдържа информация свързана с въпроса - дори частично или косвено - използвай я за отговор. Обясни с прости думи подходящи за ученици. Само ако контекстът наистина не съдържа никаква релевантна информация, отговори с: 'Нямам информация по този въпрос в наличните учебни материали.'"},
                {"role": "user", "content": f"Контекст:\n{context}\n\nВъпрос: {question}"}
            ],
            "max_tokens": 500
        }
    )
    answer = chat_res.json()["choices"][0]["message"]["content"]

    if "Нямам информация" in answer:
        return jsonify({
            "answer": answer,
            "image_url": None,
            "references": []
        })

    references = []
    for r in results:
        text = r.get("text_content") or r.get("snippet") or ""
        if not text:
            continue
        blob_url = r.get("blob_url", "")
        filename = unquote(blob_url.split("/").pop()) if blob_url else ""
        references.append({
            "text": text,
            "section": r.get("section", ""),
            "subject": r.get("subject", ""),
            "grade": r.get("grade", ""),
            "filename": filename
        })

    return jsonify({"answer": answer, "image_url": None, "references": references})

# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────

@app.route("/stats", methods=["GET"])
def stats():
    approved = list(blob_client.get_container_client("chunks-approved").list_blobs())
    pending = list(blob_client.get_container_client("chunks-pending").list_blobs())
    recent = [b.name for b in sorted(approved, key=lambda x: x.last_modified, reverse=True)[:5]]
    return jsonify({
        "approved": len(approved),
        "pending": len(pending),
        "recent": recent
    })


if __name__ == "__main__":
    app.run()
