from flask import Flask, jsonify, request
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
import os
import json

app = Flask(__name__)

blob_client = BlobServiceClient.from_connection_string(
    os.environ["STORAGE_CONN"]
)

@app.route("/")
def home():
    return "Akademiko Admin работи!"

@app.route("/pending", methods=["GET"])
def list_pending():
    container = blob_client.get_container_client("chunks-pending")
    blobs = [b.name for b in container.list_blobs()]
    return jsonify({"pending": blobs})

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
    
@app.route("/ocr/<filename>", methods=["POST"])
def ocr_file(filename):
    import requests as req
    source = blob_client.get_blob_client("textbooks-raw", filename)
    pdf_bytes = source.download_blob().readall()
    
    endpoint = os.environ["DOC_ENDPOINT"]
    key = os.environ["DOC_KEY"]
    
    url = f"{endpoint}documentintelligence/documentModels/prebuilt-layout:analyze?api-version=2024-11-30"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"}
    
    response = req.post(url, headers=headers, data=pdf_bytes)
    operation_url = response.headers["Operation-Location"]
    
    import time
    while True:
        result = req.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}).json()
        if result["status"] == "succeeded":
            break
        time.sleep(2)
    
    chunks = []
    for i, para in enumerate(result.get("analyzeResult", {}).get("paragraphs", [])):
        if len(para.get("content", "").strip()) < 30:
            continue
        chunks.append({
            "chunk_id": f"{filename}-{i:04d}",
            "source_file": filename,
            "text_content": para["content"].strip(),
            "page": para.get("boundingRegions", [{}])[0].get("pageNumber", 0)
        })
    
    output_name = filename.replace(".pdf", "_chunks.json")
    dest = blob_client.get_blob_client("chunks-pending", output_name)
    dest.upload_blob(json.dumps(chunks, ensure_ascii=False, indent=2), overwrite=True)
    
    return jsonify({"status": "ok", "chunks": len(chunks), "file": output_name})

if __name__ == "__main__":
    app.run()
