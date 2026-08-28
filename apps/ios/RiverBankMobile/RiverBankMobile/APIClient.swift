import Foundation

extension Error {
    var isRiverBankCancellation: Bool {
        if self is CancellationError { return true }
        if let urlError = self as? URLError, urlError.code == .cancelled { return true }
        let error = self as NSError
        return error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled
    }
}

enum RiverBankAPIError: LocalizedError {
    case invalidServer
    case missingToken
    case badResponse
    case server(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidServer: "服务器地址无效"
        case .missingToken: "请先填写配对令牌"
        case .badResponse: "设备返回了无法识别的数据"
        case let .server(code, message): "设备错误 \(code)：\(message)"
        }
    }
}

struct APIClient: Sendable {
    let server: String
    let token: String

    private func url(for endpoint: String) throws -> URL {
        guard var url = URL(string: server.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(url.scheme?.lowercased() ?? "") else {
            throw RiverBankAPIError.invalidServer
        }
        let endpointParts = endpoint.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        for component in endpointParts[0].split(separator: "/") {
            url.appendPathComponent(String(component))
        }
        guard endpointParts.count == 2 else { return url }
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw RiverBankAPIError.invalidServer
        }
        components.percentEncodedQuery = String(endpointParts[1])
        guard let finalURL = components.url else { throw RiverBankAPIError.invalidServer }
        return finalURL
    }

    private func request(
        _ endpoint: String,
        method: String = "GET",
        body: Data? = nil,
        idempotencyKey: String? = nil
    ) throws -> URLRequest {
        guard !token.isEmpty else { throw RiverBankAPIError.missingToken }
        var request = URLRequest(url: try url(for: endpoint))
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 45
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let idempotencyKey {
            request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key")
        }
        return request
    }

    private func decode<T: Decodable>(_ type: T.Type, request: URLRequest) async throws -> T {
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw RiverBankAPIError.badResponse
        }
        guard 200..<300 ~= http.statusCode else {
            let message = String(data: data, encoding: .utf8) ?? "未知错误"
            throw RiverBankAPIError.server(http.statusCode, message)
        }
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw RiverBankAPIError.badResponse
        }
    }

    func status() async throws -> StatusEnvelope {
        try await decode(StatusEnvelope.self, request: request("api/v1/status"))
    }

    func listTasks(limit: Int = 100) async throws -> TaskListEnvelope {
        try await decode(
            TaskListEnvelope.self,
            request: request("api/v1/tasks?limit=\(limit)")
        )
    }

    func task(id: String) async throws -> RemoteTask {
        try await decode(
            TaskEnvelope.self,
            request: request("api/v1/tasks/\(id)")
        ).task
    }

    func createTask(title: String, prompt: String, kind: String) async throws -> RemoteTask {
        let payload: [String: String] = [
            "title": title,
            "prompt": prompt,
            "kind": kind,
            "source": "ios",
            "device_name": "iOS device"
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        return try await decode(
            CreateTaskEnvelope.self,
            request: request(
                "api/v1/tasks",
                method: "POST",
                body: body,
                idempotencyKey: UUID().uuidString
            )
        ).task
    }

    func answer(taskID: String, answer: String) async throws -> RemoteTask {
        let body = try JSONSerialization.data(withJSONObject: ["answer": answer])
        return try await decode(
            TaskEnvelope.self,
            request: request("api/v1/tasks/\(taskID)/answer", method: "POST", body: body)
        ).task
    }

    func cancel(taskID: String) async throws -> RemoteTask {
        let body = try JSONSerialization.data(withJSONObject: [:])
        return try await decode(
            TaskEnvelope.self,
            request: request("api/v1/tasks/\(taskID)/cancel", method: "POST", body: body)
        ).task
    }

    func listReports(limit: Int = 200) async throws -> ReportListEnvelope {
        try await decode(
            ReportListEnvelope.self,
            request: request("api/v1/reports?limit=\(limit)")
        )
    }

    func report(id: String) async throws -> ReportContentEnvelope {
        try await decode(
            ReportContentEnvelope.self,
            request: request("api/v1/reports/\(id)")
        )
    }

    func download(report: ReportRecord) async throws -> URL {
        let (temporary, response) = try await URLSession.shared.download(
            for: request("api/v1/reports/\(report.id)/download")
        )
        guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
            throw RiverBankAPIError.badResponse
        }
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent(report.filename)
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporary, to: destination)
        return destination
    }
}
