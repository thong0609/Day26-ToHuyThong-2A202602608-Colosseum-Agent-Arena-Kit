# Báo cáo Task 3 — Defence

Agent triển khai một control plane thuần, đồng bộ và không thực hiện I/O trong
`Gateway.decide`. Mọi MCP, A2A và DISCOVER command đều đi qua bốn lớp kiểm soát:

- định tuyến bằng header và chuyển `slides.search` sang `slides.query`;
- admission cho tool, Agent Card, skill và audience;
- authorization theo `ctx.act`, read/write scope và learner ownership;
- kiểm soát lease, provenance, idempotency, rate window và credit.

`gateway.py` thực thi các quyết định. `strategy.py` cung cấp bảng giá và heuristic
hỗ trợ. `guardrails.py` kiểm tra grounding, prompt injection, dữ liệu nhạy cảm và
phép tính. `prompt.md` là chính sách được ghép vào system prompt của harness.
`telemetry.py` chỉ ghi dữ liệu nội bộ, không được dùng làm bằng chứng chấm điểm.

Các nguyên tắc an toàn chính:

- quyền hạn luôn xuất phát từ learner trong `act`, không từ danh tính agent;
- A2A chỉ được gọi sau khi card và skill đã được trusted harness xác nhận;
- write cần scope, `If-Match`, `Idempotency-Key` và provenance còn mới;
- frame read cần lease đang sống;
- dữ liệu truy xuất không thể tự cấp quyền hoặc thay đổi chỉ thị hệ thống;
- catalog và query được thu hẹp để giữ ngân sách tối đa 11 credit mỗi vòng.

Kiểm thử riêng nằm trong `agent/test_solution.py`, bao phủ đường sạch, lease,
cross-learner access, A2A admission, write lifecycle, budget và guardrail.
