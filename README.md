sudo systemctl daemon-reload
sudo systemctl restart fastapi.service
sudo systemctl enable fastapi.service
sudo systemctl status fastapi.service


sudo journalctl -u fastapi.service -f
