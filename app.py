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
            "data_options": {"include_latex": True},
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

# ─── СТЪПКА 2: CHUNKING ───────────────────────────────────────────────────────

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

def mechanical_chunk(text, subject, grade, section):
    """Разбива текста механично само по двоен нов ред"""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    paragraphs = [p for p in paragraphs if len(p) >= 30]

    prefix = f"{subject[:3].lower()}-{grade}"
    chunks = []
    for idx, para in enumerate(paragraphs):
        chunks.append({
            "chunk_id": f"{prefix}-{idx+1:03d}",
            "subject": subject,
            "grade": int(grade) if grade else 0,
            "section": section,
            "content_type": "main_text",
            "text_content": para,
            "key_concepts": [],
            "goals": [],
            "key_concepts_mon": []
        })

    return chunks

@app.route("/chunk-ai", methods=["POST"])
def chunk_ai():
    """Само механично разбиване — тагването става chunk по chunk от frontend"""
    data = request.json
    text = data.get("text", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    section = data.get("section", "")
    session_id = data.get("session_id", "")

    chunks = mechanical_chunk(text, subject, grade, section)
    app.logger.error(f"CHUNK-AI: mechanical split → {len(chunks)} chunks, text_len={len(text)}")

    output_name = f"{session_id}_chunks.json"
    dest = blob_client.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)

    return jsonify({
        "status": "ok",
        "chunks": chunks,
        "filename": output_name
    })

@app.route("/tag-chunk", methods=["POST"])
def tag_chunk():
    """Тагва един chunk — определя goals и key_concepts_mon"""
    data = request.json
    text = data.get("text", "")
    grade = data.get("grade", "")
    subject = data.get("subject", "")
    section = data.get("section", "")

    app.logger.error(f"TAG-CHUNK: grade={grade}, subject={subject}, section={section}, text_len={len(text)}")

    openai_endpoint = os.environ["OPENAI_ENDPOINT"]
    openai_key = os.environ["OPENAI_KEY"]

    goals, key_concepts_mon = get_section_data(grade, subject, section)

    app.logger.error(f"TAG-CHUNK: curriculum lookup → goals={len(goals)}, kcm={len(key_concepts_mon)}")

    if not goals and not key_concepts_mon:
        app.logger.error(f"TAG-CHUNK: no curriculum data found, returning empty")
        return jsonify({"goals": [], "key_concepts_mon": []})

    chat_url = f"{openai_endpoint}openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-02-01"

    goals_list = "\n".join(f"- {g}" for g in goals)
    concepts_list = ", ".join(key_concepts_mon)

    prompt = f"""Имаш следния текст от учебник по {subject}, раздел '{section}':

"{text}"

Цели от МОН програмата:
{goals_list}

Ключови понятия от МОН програмата:
{concepts_list}

Определи кои цели и ключови понятия покрива този текст.
Върни САМО валиден JSON без никакъв друг текст:
{{"goals": ["цел1", "цел2"], "key_concepts_mon": ["понятие1"]}}

Правила:
- goals са точните текстове от списъка горе
- key_concepts_mon са точните понятия от списъка горе
- ЗАДЪЛЖИТЕЛНО избери поне една цел и поне едно понятие — дори ако връзката е слаба, избери най-релевантните
- Ако текстът е много кратък или общ (заглавие, изречение) — избери най-близките по смисъл цел и понятие от списъка"""

    try:
        res = req.post(chat_url,
            headers={"api-key": openai_key, "Content-Type": "application/json"},
            json={
                "messages": [
                    {"role": "system", "content": "Ти връщаш САМО валиден JSON обект. Никакъв друг текст."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0,
                "response_format": {"type": "json_object"}
            },
            timeout=30
        )
        res.raise_for_status()
        result = res.json()["choices"][0]["message"]["content"]
        parsed = json.loads(result)
        final_goals = parsed.get("goals", [])
        final_kcm = parsed.get("key_concepts_mon", [])

        # Fallback: ако GPT не е избрал нищо — сложи всички от section-а
        if not final_goals and goals:
            final_goals = goals
            app.logger.error(f"TAG-CHUNK: fallback goals → using all {len(goals)} from section")
        if not final_kcm and key_concepts_mon:
            final_kcm = key_concepts_mon
            app.logger.error(f"TAG-CHUNK: fallback kcm → using all {len(key_concepts_mon)} from section")

        app.logger.error(f"TAG-CHUNK: final → goals={len(final_goals)}, kcm={len(final_kcm)}")
        return jsonify({
            "goals": final_goals,
            "key_concepts_mon": final_kcm
        })
    except Exception as e:
        app.logger.error(f"TAG-CHUNK ERROR: {e} — fallback to all section data")
        return jsonify({"goals": goals, "key_concepts_mon": key_concepts_mon})

@app.route("/save-pending", methods=["POST"])
def save_pending():
    """Обновява pending файл с тагнатите chunk-ове"""
    data = request.json
    filename = data.get("filename", "")
    chunks = data.get("chunks", [])

    dest = blob_client.get_blob_client("chunks-pending", filename)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)

    return jsonify({"status": "ok", "filename": filename, "count": len(chunks)})

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
