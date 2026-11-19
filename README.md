# QuantLeaf — Strategy 5: Sector Leaders Intraday Dashboard

A modern, minimal, high-performance intraday trading dashboard built directly according to the **QuantLeaf Strategy 5 Developer Specification** (`QuantLeaf_Strategy5_Sector_Leaders_Intraday_Developer_Spec.pdf`).

Now integrated directly with **Angel One SmartAPI**:
- **Root API**: `https://apiconnect.angelone.in`
- **Market Quote API**: `https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/`
- **Documentation**: [SmartAPI Introduction](https://smartapi.angelone.in/docs/Introduction)

Supports **Poppins font family**, seamless **Dark Mode** & **Light Mode**, real-time live price quote streaming, 12-check rule scanner inspector, sector persistence engine, and full REST API compliance (Section 14).

---

## 🚀 Live Access Links

- **Live Local Server Link:**  
  👉 **[http://localhost:8081](http://localhost:8081)**
- **Direct Browser File Link:**  
  👉 **[Open index.html](file:///Users/kevalpatel/Desktop/Treading/index.html)**

---

## ⚡ Angel One SmartAPI Integration

### 1. Market Data Quote Endpoint
The backend and frontend continuously query the live market quote API:
```http
POST https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/
Headers:
  Authorization: Bearer <jwtToken>
  Content-Type: application/json
  X-PrivateKey: <ANGEL_API_KEY>
  X-UserType: USER
  X-SourceID: WEB
Payload:
  {
    "mode": "FULL",
    "exchangeTokens": {
      "NSE": ["99926000", "3456", "20374", "11630", "526", "16669", "2475", "2885"]
    }
  }
```

### 2. Pure Python RFC 6238 TOTP Engine
Authentication uses standard library HMAC-SHA1 TOTP generation without third-party dependencies:
```http
POST https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword
Payload:
  {
    "clientcode": "<ANGEL_CLIENT_ID>",
    "password": "<ANGEL_PIN>",
    "totp": "<6_DIGIT_TOTP>"
  }
```

### 3. Setting Your Credentials
You can connect in two easy ways:
1. **Interactive UI**: Click the **SmartAPI: Active** pill in the top header to enter your API Key, Client ID, PIN, and TOTP Secret.
2. **Environment File (`.env`)**:
   ```env
   ANGEL_API_KEY=your_smartapi_key
   ANGEL_CLIENT_ID=your_client_id
   ANGEL_PIN=your_account_pin
   ANGEL_TOTP_SECRET=your_totp_secret
   ```

---

## 🎨 Features & Highlights

- **Poppins Typography**: Premium typography styling across brand headers, cards, tables, and modal drawers with tabular numeric alignment for zero-jitter ticker feeds.
- **Flashing Tickers**: Real-time green/red highlight animations on each price update.
- **Nifty 50 Stance Badge**: Strong ($\ge +0.40\%$), Mild ($0\text{ to } +0.40\%$), Neutral ($-0.35\%\text{ to } 0\%$), Bearish ($< -0.35\%$).
- **Sector Strength Board**: Top 2 sectors highlighted only after 3 consecutive 5-minute candle closes ($persistence = 3$).
- **12-Point Hard Rules Scanner**: Click **12/12 Passed** to inspect every condition evaluated on candle close.
- **Emergency Halt System**: Owner authorization prompt with audit logging.
