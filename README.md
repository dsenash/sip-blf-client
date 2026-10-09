python3 main.py -h
usage: main.py [-h] [--config CONFIG] [--blf] [--no-call] [sip_uri]

SIP client for sending INVITE and streaming WAV over RTP

positional arguments:
  sip_uri          SIP URI (e.g. sip:101@192.168.0.89:5060;transport=udp)

options:
  -h, --help       show this help message and exit
  --config CONFIG  Path to config file (default: config.json)
  --blf            Subscribe to the BLF buttons in the config and enable the console
  --no-call        Do not place the startup call, only watch BLF


  cat config.json 
{
    "local_ip": "0.0.0.0",
    "local_port": 5080,
    "transport": "udp",
    "target_user": "202",
    "target_host": "192.168.8.134",
    "target_port": 5060,
    "wav_file": "piano2.wav",
    "rtp_port": 10000,
    "codec": "PCMU",
    "sample_rate": 8000,
    "call_duration": 30,
    "user_agent": "PythonSIP/1.0",
    "from_user": "100",
    "display_name": "SIP Client",
    "sip_call_timeout": 30,
    "blf": {
        "enabled": true,
        "domain": "default",
        "expires": 3600,
        "refresh": 1800,
        "buttons": [
            {"name": "201", "number": "201", "label": "Kitchen"},
            {"name": "202", "number": "202", "label": "Fonarik"},
            {"name": "203", "number": "203", "label": "Banya"},
            {"name": "204", "number": "204", "label": "Anna"},
            {"name": "205", "number": "205", "label": "Podval"}
        ]
    }
}



python3 main.py --no-call
[+] Target: sip:202@192.168.8.134:5060 (protocol: udp)
[BLF] SUBSCRIBE 201 -> sip:201@default (expires=3600)
[BLF] SUBSCRIBE 202 -> sip:202@default (expires=3600)
[BLF] SUBSCRIBE 203 -> sip:203@default (expires=3600)
[BLF] SUBSCRIBE 204 -> sip:204@default (expires=3600)
[BLF] SUBSCRIBE 205 -> sip:205@default (expires=3600)
blf> [*] Console ready. Type 'help' for commands, Ctrl+C or 'quit' to exit.
help
blf> Commands:
  press <name>    Call the extension on a BLF button
  blf list        Show the current state of every BLF button
  blf refresh     Re-subscribe to all BLF buttons now
  help            Show this help
  ?               Same as 'help'
  hangup          Hang up the active call
  quit            Hang up, unsubscribe and exit
