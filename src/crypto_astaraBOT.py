from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({'status':'ok','service':'crypto_astaraBOT'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
