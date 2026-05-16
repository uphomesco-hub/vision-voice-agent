import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import RepairAssistant from "@/pages/RepairAssistant";

function App() {
  return (
    <div className="App">
      <BrowserRouter basename={process.env.PUBLIC_URL || "/"}>
        <Routes>
          <Route path="/" element={<RepairAssistant />} />
          <Route path="/repair" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </div>
  );
}

export default App;
