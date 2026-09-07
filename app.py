from flask import Flask

app = Flask(__name__)


@app.get("/")
def home():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Team System</title>
  <style>
    body {
      margin: 0;
      padding: 48px 20px;
      font-family: Arial, sans-serif;
      background: #f3f5fa;
      color: #182235;
    }

    main {
      max-width: 720px;
      margin: auto;
      padding: 32px;
      background: white;
      border-radius: 16px;
      box-shadow: 0 8px 30px #0000000d;
    }

    h1 {
      margin-top: 0;
    }

    p {
      line-height: 1.6;
    }

    button {
      padding: 12px 18px;
      border: 0;
      border-radius: 8px;
      background: #2457df;
      color: white;
      font-size: 16px;
      cursor: pointer;
    }

    button:disabled {
      opacity: 0.6;
      cursor: wait;
    }

    #status {
      min-height: 24px;
      font-weight: bold;
    }
  </style>
</head>
<body>
  <main>
    <h1>Team System</h1>
    <p>Welcome! This application is running online.</p>
    <p>Use the button below to check the connection to the server.</p>

    <button id="check" onclick="checkServer()">
      Check connection
    </button>

    <p id="status" role="status"></p>
  </main>

  <script>
    async function checkServer() {
      const button = document.getElementById("check");
      const status = document.getElementById("status");

      button.disabled = true;
      status.textContent = "Checking...";

      try {
        const response = await fetch("/api/health");

        if (!response.ok) {
          throw new Error("Request failed");
        }

        const data = await response.json();
        status.textContent = data.message;
      } catch (error) {
        status.textContent = "Could not connect. Please try again.";
      } finally {
        button.disabled = false;
      }
    }
  </script>
</body>
</html>
"""


@app.get("/api/health")
def health():
    return {"message": "Success! Your application is online."}
