import requests

TOKEN = "EAAnCPODMFHgBRFKYjzy3TAqpZABcM3ZAmZBmRKgqYfmjE7wS9DYH93mdZB7HRZCZBpV7HETpTG4TXzTW6LFcBrAAonq86cp4lV4e3ZCkkWhZAO3jXfwUx2EwahwLSPZBGsSm5ALsUEnTFf72JIic5ZBI2ZAIFh4lb2GH6koS5LUPbxEScfqqpA4WgJOP3dEZAbZB6HWsr9ZAVxwcIJP48pCvIITsGhBIzcbHJinoZCaS74p134aDqqwn89k2SsRcD8RdMWyPlYBAUJtNZA7SWMbRrbZBWwxBHYwZDZD"
PHONE_NUMBER_ID = "1088821460973757"

def send_whatsapp_message(to, message):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }

    response = requests.post(url, headers=headers, json=data)
    return response.json()