nohup /opt/miniconda3/envs/medical/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8083 > chat_user.log 2>&1 &
