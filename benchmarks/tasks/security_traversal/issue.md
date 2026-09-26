Security: `read_upload("../secrets.txt")` returns the contents of a file outside the uploads directory.
`read_upload(name)` must raise `ValueError` for any name that resolves outside `UPLOAD_DIR`
(including absolute paths). Normal names, including files in sub-folders like `2024/report.txt`, must keep working.
