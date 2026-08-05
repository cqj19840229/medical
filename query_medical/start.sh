nohup /opt/miniconda3/envs/medical/bin/python -m uvicorn main:app \
  --host 0.0.0.0 \
  --port 8888 \
  >>  querymedical.log 2>&1 &
