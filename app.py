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

@app.route("/upload-ocr", methods=["POST"])
def upload_ocr():
    file = request.files.get("file")
    subject = request.form.get("subject", "")
    grade = request.form.get("grade", "")
    publisher = request.form.get("publisher", "")
    chapter = request.form.get("chapter", "")

    if not file:
        return jsonify({"status": "error", "message": "Няма файл"}), 400

    filename = file.filename
    file_bytes = file.read()

    upload_blob = blob_client.get_blob_client("textbooks-raw", filename)
    upload_blob.upload_blob(file_bytes, overwrite=True)

    endpoint = os.environ["DOC_ENDPOINT"]
    key = os.environ["DOC_KEY"]

    if filename.lower().endswith(".png"):
        content_type = "image/png"
    elif filename.lower().endswith(".jpg") or filename.lower().endswith(".jpeg"):
        content_type = "image/jpeg"
    else:
        content_type = "application/pdf"

    url = f"{endpoint}documentintelligence/documentModels/prebuilt-layout:analyze?api-version=2024-11-30"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": content_type}
    response = req.post(url, headers=headers, data=file_bytes)

    if "Operation-Location" not in response.headers:
        return jsonify({"status": "error", "message": "OCR грешка"}), 500

    operation_url = response.headers["Operation-Location"]

    while True:
        result = req.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}).json()
        if result["status"] == "succeeded":
            break
        if result["status"] == "failed":
            return jsonify({"status": "error", "message": "OCR неуспешен"}), 500
        time.sleep(2)

    chunks = []
    for i, para in enumerate(result.get("analyzeResult", {}).get("paragraphs", [])):
        text = para.get("content", "").strip()
        if len(text) < 30:
            continue
        chunks.append({
            "chunk_id": f"{filename}-{i:04d}",
            "source_file": filename,
            "subject": subject,
            "grade": int(grade) if grade.isdigit() else grade,
            "publisher": publisher,
            "chapter": chapter,
            "text_content": text,
            "page": para.get("boundingRegions", [{}])[0].get("pageNumber", 0)
        })

    output_name = filename.rsplit(".", 1)[0] + "_chunks.json"
    dest = blob_client.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)

    return jsonify({"status": "ok", "chunks": len(chunks), "file": output_name})

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
