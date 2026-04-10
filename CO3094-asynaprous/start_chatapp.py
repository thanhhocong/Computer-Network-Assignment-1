"""
start_chatapp
~~~~~~~~~~~~~~~~~

Starts the BK-Messenger chat application.

This is the main entry point you'd use to demo the assignment:
  python start_chatapp.py --server-port 8000

Then open http://127.0.0.1:8000/login in a browser.
Use two different browsers (or incognito tabs) to test
two users chatting with each other.
"""

import argparse
import socket

from apps import create_chatapp

PORT = 8000

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='ChatApp',
        description='Hybrid P2P Chat Application on AsynapRous',
        epilog='CO3094 Computer Network - Assignment 1'
    )
    parser.add_argument(
        '--server-ip',
        type=str,
        default='0.0.0.0',
        help='IP address to bind the server. Default: 0.0.0.0'
    )
    parser.add_argument(
        '--server-port',
        type=int,
        default=PORT,
        help='Port number to bind the server. Default: {}'.format(PORT)
    )

    args = parser.parse_args()
    ip = args.server_ip
    port = args.server_port

    print("=" * 60)
    print("  BK Discordmess Chat Application")
    print("  HCMC University of Technology (HCMUT)")
    print("  CO3094 - Computer Network - Assignment 1")
    print("=" * 60)
    print("  Server: http://{}:{}".format(
        "127.0.0.1" if ip == "0.0.0.0" else ip, port))
    print("  Login:  http://{}:{}/login".format(
        "127.0.0.1" if ip == "0.0.0.0" else ip, port))
    print("  Admin:  http://{}:{}/admin".format(
        "127.0.0.1" if ip == "0.0.0.0" else ip, port))
    print("=" * 60)

    create_chatapp(ip, port)
