#!/usr/bin/python3

import socket
import configparser
import openai
import time
import textwrap
import asyncio

# Configuration and Initialization
config = configparser.ConfigParser()
config.read('/path/to/config/config.ini')
openai.api_key = config['openai']['api_key']

server = "irc.newnet.net"
channel = "#channel"
botnick = "Nickname"
nspass = "NICKSERV_PASSWORD"
modeset = "+B"

# Connect to IRC
irc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
irc.connect((server, 6667))
irc.send(bytes("USER "+ botnick +" "+ botnick +" "+ botnick + " " + botnick + "\n", "UTF-8"))
irc.send(bytes("NICK "+ botnick +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("NS IDENTIFY "+ nspass +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("MODE "+ botnick +" "+ modeset +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("JOIN "+ channel +" "+ server +"\n", "UTF-8"))

# Message Handling
async def handle_messages():
    user_contexts = {}
    while True:
        text = irc.recv(2040).decode("UTF-8")
        print(text)

        if text.find('PING') != -1:
            irc.send(bytes('PONG ' + text.split()[1] + '\r\n', "UTF-8"))

        if "PRIVMSG" in text:
            username, message = parse_message(text)
            if username and message:
                if message.startswith('!'):
                    await handle_command(username, message)
                else:
                    await handle_conversation(username, message, user_contexts)

async def handle_conversation(username, message, user_contexts):
    context = user_contexts.get(username, [])
    context.append({"role": "user", "content": message})
    if len(context) > 10:  # Adjust history length as needed
        context.pop(0)

    response = await get_openai_response(context)
    reply = response['choices'][0]['message']['content']
    print(reply)

    for chunk in textwrap.wrap(reply, 200):
        irc.send(bytes("PRIVMSG "+ channel +" :"+ chunk +"\n", "UTF-8"))
    user_contexts[username] = context

async def get_openai_response(context):
    return openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=context,
        temperature=0.85,
        max_tokens=150,
        top_p=1,
        frequency_penalty=0.5,
        presence_penalty=1.0,
    )

def parse_message(text):
    if botnick in text:
        parts = text.split('!')
        username = parts[0].split(':')[1]
        message = parts[1].split(':')[1]
        return username, message
    return None, None

async def handle_command(username, command):
    if command.startswith('!help'):
        send_message(f"Hello {username}, I'm a GPT-powered IRC bot. Ask me anything!")
    # Add more commands as needed

def send_message(message):
    for chunk in textwrap.wrap(message, 200):
        irc.send(bytes("PRIVMSG "+ channel +" :"+ chunk +"\n", "UTF-8"))

# Main
async def main():
    await handle_messages()

if __name__ == "__main__":
    asyncio.run(main())
