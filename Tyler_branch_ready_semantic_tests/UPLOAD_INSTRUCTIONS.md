# Upload semantic tests to the Tyler branch

The connected GitHub integration returned `403 Resource not accessible by integration` for both Git Trees and Contents writes. It made no repository changes.

Manual upload:
1. Unzip this archive locally.
2. Open https://github.com/doug-tranz/CS-Research-II-Fall-26/tree/Tyler and make sure branch **Tyler** is selected.
3. Choose **Add file → Upload files**. Drag the **semantic_tests** folder (or create that folder and upload its six individual files).
4. Commit directly to branch **Tyler**. Do not upload this ZIP as-is expecting GitHub to extract it.

For direct ChatGPT edits, reconnect/reconfigure the GitHub integration with repository **Contents: Read and write** permission for doug-tranz/CS-Research-II-Fall-26.

Usage: See semantic_tests/README.md. The Python adapter needs harness.py and registration.py from branch Aneesh-Anand, and optionally registration_input.schema.json from Daquan-1 for schema checking. No LLM runs are included; labels still need team review.
