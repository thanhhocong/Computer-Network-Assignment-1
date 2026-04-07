# Computer-Network-Assignment-1
This is the work of group 7 CC03 for assignment 1 of Computer Network.

## Manual
### Clone the repo:
```bash
git clone https://github.com/thanhhocong/Computer-Network-Assignment-1.git
```
### CD to the folder:
```bash
cd ./Computer-Network-Assignment-1/CO3094-asynaprous/
```

### Make sure the server and port is freed

```bash
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Write-Host "Port 8000 freed"
```

### Run the app

```bash
python start_chatapp.py --server-port 8000
```
### Go to web and join

You can either join to the server host ip: 127.0.0.1:8000 or get the ipv4 of the machine by:

```bash
ipconfig
```
and find for 
```bash 
Wireless LAN adapter Wi-Fi:
```
to see the ipv4 and then you can join by http://"YOUR ipv4 HERE":8000/login