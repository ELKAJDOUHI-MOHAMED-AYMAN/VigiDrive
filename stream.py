import sys
sys.path.insert(0, "/usr/lib/python3/dist-packages")
from flask import Flask, Response
from picamera2 import Picamera2
import io, time

app = Flask(__name__)
picam2 = Picamera2()
picam2.configure(picam2.create_video_configuration(main={"size": (640, 480)}))
picam2.start()
time.sleep(1)

def generate():
    while True:
        stream = io.BytesIO()
        picam2.capture_file(stream, format='jpeg')
        stream.seek(0)
        frame = stream.read()
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.05)

@app.route('/')
def index():
    return '<html><body style="margin:0;background:#000"><img src="/stream" width="100%"></body></html>'

@app.route('/stream')
def stream():
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)