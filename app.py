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
    from ocr import ocr_and_chunk
    count = ocr_and_chunk(filename)
    return jsonify({"status": "ok", "chunks": count, "file": filename})

if __name__ == "__main__":
    app.run()
