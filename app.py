from flask import Flask, jsonify, request, render_template
from azure.storage.blob import BlobServiceClient
import os
import json
import requests as req
import time

app = Flask(__name__)

blob_client = BlobServiceClient.from_connection_string(
    os.environ["STORAGE_CONN"]
)

@app.route("/")
def home():
    return render_template("index.html")

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

# ─── СТЪПКА 1: OCR ────────────────────────────────────────────────────────────

@app.route("/ocr-batch", methods=["POST"])
def ocr_batch():
    files = request.files.getlist("files")
    subject = request.form.get("subject", "")
    grade = request.form.get("grade", "")
    publisher = request.form.get("publisher", "")
    chapter = request.form.get("chapter", "")
    session_id = request.form.get("session_id", "")

    if not files:
        return jsonify({"status": "error", "message": "Няма файлове"}), 400

    endpoint = os.environ["DOC_ENDPOINT"]
    key = os.environ["DOC_KEY"]
    all_text = []

    for file in files:
        filename = file.filename
        file_bytes = file.read()

        upload_blob = blob_client.get_blob_client("textbooks-raw", filename)
        upload_blob.upload_blob(file_bytes, overwrite=True)

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
            continue

        operation_url = response.headers["Operation-Location"]
        while True:
            result = req.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}).json()
            if result["status"] == "succeeded":
                break
            if result["status"] == "failed":
                break
            time.sleep(2)

        if result["status"] == "succeeded":
            page_text = result.get("analyzeResult", {}).get("content", "")
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
        "chapter": chapter
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

@app.route("/chunk-ai", methods=["POST"])
def chunk_ai():
    data = request.json
    text = data.get("text", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    publisher = data.get("publisher", "")
    chapter = data.get("chapter", "")
    session_id = data.get("session_id", "")

    openai_endpoint = os.environ["OPENAI_ENDPOINT"]
    openai_key = os.environ["OPENAI_KEY"]

    system_prompt = """Ти си експерт по образователно съдържание. Анализирай предоставения учебен текст и го раздели на смислени chunk-ове подходящи за RAG система.

За всеки chunk създай JSON обект със следните полета:
- chunk_id: уникален идентификатор (напр. "предмет-клас-номер")
- subject: предмет (определи от контекста или използвай предоставения)
- grade: клас като число (определи от контекста или използвай предоставения)
- chapter: глава или тема
- content_type: тип съдържание (main_text, glossary, questions, exercise, table, example)
- section: заглавие на секцията
- text_content: самият текст на chunk-а
- key_concepts: масив с ключови понятия от chunk-а

Правила за chunk-ване:
- Всеки chunk трябва да е семантично завършен и самостоятелен
- Минимална дължина: 50 символа
- Максимална дължина: 800 символа
- Не разделяй изречения в средата
- Групирай свързани изречения заедно
- Речникови дефиниции да са отделни chunk-ове
- Въпроси и задачи да са отделни chunk-ове

Върни САМО валиден JSON масив без никакъв друг текст."""

    user_prompt = f"""Предмет: {subject}
Клас: {grade}
Тема: {chapter}

Текст за chunk-ване:
{text}"""

    chat_url = f"{openai_endpoint}openai/deployments/gpt-4.1/chat/completions?api-version=2024-02-01"
    chat_res = req.post(chat_url,
        headers={"api-key": openai_key, "Content-Type": "application/json"},
        json={
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 4000,
            "temperature": 0.1
        }
    )
    app.logger.info(f"GPT raw response: {chat_res.json()}")
    raw = chat_res.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        chunks = json.loads(raw)
    except Exception:
        chunks = []

    output_name = f"{session_id}_chunks.json"
    dest = blob_client.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)

    return jsonify({
        "status": "ok",
        "chunks": chunks,
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

    search_url = f"{search_endpoint}indexes/akademiko-knowledge-source-index/docs/search?api-version=2024-07-01"
    search_body = {
        "search": question,
        "vectorQueries": [{"kind": "vector", "vector": vector, "fields": "snippet_vector", "k": 5}],
        "select": "text_content,blob_url",
        "top": 3
    }
    search_res = req.post(search_url,
        headers={"api-key": search_key, "Content-Type": "application/json"},
        json=search_body
    )
    results = search_res.json().get("value", [])
    context = "\n\n".join([r.get("text_content") or r.get("snippet") or "" for r in results])

    if not context.strip():
        return jsonify({"answer": "Нямам информация по този въпрос в наличните учебни материали."})

    chat_url = f"{openai_endpoint}openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-02-01"
    chat_res = req.post(chat_url,
        headers={"api-key": openai_key, "Content-Type": "application/json"},
        json={
            "messages": [
                {"role": "system", "content": "Отговаряй на български САМО на база предоставеното учебно съдържание. Ако контекстът не съдържа достатъчно информация по въпроса, отговори само с: 'Нямам информация по този въпрос в наличните учебни материали.' Не използвай собствени знания извън предоставения контекст."},
                {"role": "user", "content": f"Контекст:\n{context}\n\nВъпрос: {question}"}
            ],
            "max_tokens": 500
        }
    )
    answer = chat_res.json()["choices"][0]["message"]["content"]
    return jsonify({"answer": answer})

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
