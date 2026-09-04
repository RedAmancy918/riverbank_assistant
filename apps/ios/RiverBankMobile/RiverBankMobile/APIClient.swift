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
        case .missingToken: "请先登录 RiverBank 账号"
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
        idempotencyKey: String? = nil,
        requiresAuth: Bool = true,
        bearerOverride: String? = nil
    ) throws -> URLRequest {
        if requiresAuth && token.isEmpty && bearerOverride == nil {
            throw RiverBankAPIError.missingToken
        }
        var request = URLRequest(url: try url(for: endpoint))
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 45
        if let bearer = bearerOverride ?? (requiresAuth ? token : nil), !bearer.isEmpty {
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        }
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

    func authConfig() async throws -> AuthConfigEnvelope {
        try await decode(
            AuthConfigEnvelope.self,
            request: request("api/v1/auth/config", requiresAuth: false)
        )
    }

    func login(username: String, password: String, deviceName: String) async throws -> AuthSessionEnvelope {
        let body = try JSONSerialization.data(withJSONObject: [
            "username": username,
            "password": password,
            "device_name": deviceName
        ])
        return try await decode(
            AuthSessionEnvelope.self,
            request: request(
                "api/v1/auth/login",
                method: "POST",
                body: body,
                requiresAuth: false
            )
        )
    }

    func register(
        username: String,
        password: String,
        displayName: String,
        registrationPasscode: String,
        deviceName: String
    ) async throws -> AuthSessionEnvelope {
        let body = try JSONSerialization.data(withJSONObject: [
            "username": username,
            "password": password,
            "display_name": displayName,
            "registration_passcode": registrationPasscode,
            "device_name": deviceName
        ])
        return try await decode(
            AuthSessionEnvelope.self,
            request: request(
                "api/v1/auth/register",
                method: "POST",
                body: body,
                requiresAuth: false
            )
        )
    }

    func bootstrap(
        username: String,
        password: String,
        displayName: String,
        deviceName: String,
        credential: String
    ) async throws -> AuthSessionEnvelope {
        let body = try JSONSerialization.data(withJSONObject: [
            "username": username,
            "password": password,
            "display_name": displayName,
            "device_name": deviceName
        ])
        return try await decode(
            AuthSessionEnvelope.self,
            request: request(
                "api/v1/auth/bootstrap",
                method: "POST",
                body: body,
                requiresAuth: false,
                bearerOverride: credential
            )
        )
    }

    func currentUser() async throws -> AuthUserEnvelope {
        try await decode(AuthUserEnvelope.self, request: request("api/v1/auth/me"))
    }

    func logout() async throws {
        let body = try JSONSerialization.data(withJSONObject: [:])
        _ = try await decode(
            OKEnvelope.self,
            request: request("api/v1/auth/logout", method: "POST", body: body)
        )
    }

    func changePassword(current: String, new: String) async throws {
        let body = try JSONSerialization.data(withJSONObject: [
            "current_password": current,
            "new_password": new
        ])
        _ = try await decode(
            OKEnvelope.self,
            request: request("api/v1/auth/change-password", method: "POST", body: body)
        )
    }

    func chatAttachment(_ attachment: ChatAttachment) async throws -> Data {
        let endpoint = "api/v1/chats/\(attachment.conversationId)/attachments/\(attachment.id)"
        let (data, response) = try await URLSession.shared.data(for: request(endpoint))
        guard let http = response as? HTTPURLResponse else {
            throw RiverBankAPIError.badResponse
        }
        guard 200..<300 ~= http.statusCode else {
            let message = String(data: data, encoding: .utf8) ?? "未知错误"
            throw RiverBankAPIError.server(http.statusCode, message)
        }
        guard data.count <= 20 * 1024 * 1024 else {
            throw RiverBankAPIError.server(413, "图片文件过大")
        }
        return data
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

    func createTask(
        title: String,
        prompt: String,
        kind: String,
        outputFormat: String
    ) async throws -> RemoteTask {
        let payload: [String: String] = [
            "title": title,
            "prompt": prompt,
            "kind": kind,
            "output_format": outputFormat,
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

    func taskArtifactData(taskID: String) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(
            for: request("api/v1/tasks/\(taskID)/artifact")
        )
        guard let http = response as? HTTPURLResponse else {
            throw RiverBankAPIError.badResponse
        }
        guard 200..<300 ~= http.statusCode else {
            let message = String(data: data, encoding: .utf8) ?? "未知错误"
            throw RiverBankAPIError.server(http.statusCode, message)
        }
        guard data.count <= 32 * 1024 * 1024 else {
            throw RiverBankAPIError.server(413, "任务成果文件过大")
        }
        return data
    }

    func downloadArtifact(task: RemoteTask) async throws -> URL {
        let (temporary, response) = try await URLSession.shared.download(
            for: request("api/v1/tasks/\(task.id)/artifact")
        )
        guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
            throw RiverBankAPIError.badResponse
        }
        let filename = (task.artifactFilename ?? "").isEmpty
            ? "riverbank-task-\(task.id.prefix(8))"
            : task.artifactFilename!
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent(filename)
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporary, to: destination)
        return destination
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

    func listChats(limit: Int = 100) async throws -> ChatConversationListEnvelope {
        try await decode(
            ChatConversationListEnvelope.self,
            request: request("api/v1/chats?limit=\(limit)")
        )
    }

    func createChat(title: String = "") async throws -> ChatConversation {
        let payload: [String: String] = [
            "title": title,
            "source": "ios",
            "device_name": "iPhone"
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        return try await decode(
            ChatConversationEnvelope.self,
            request: request("api/v1/chats", method: "POST", body: body)
        ).conversation
    }

    func deleteChat(id: String) async throws {
        _ = try await decode(
            DeleteChatEnvelope.self,
            request: request("api/v1/chats/\(id)", method: "DELETE")
        )
    }

    func chatMessages(conversationID: String) async throws -> ChatMessagesEnvelope {
        try await decode(
            ChatMessagesEnvelope.self,
            request: request("api/v1/chats/\(conversationID)/messages")
        )
    }

    func sendChatMessage(
        conversationID: String,
        content: String,
        attachments: [ChatUploadAttachment] = []
    ) async throws -> ChatTurnEnvelope {
        if attachments.isEmpty {
            let body = try JSONSerialization.data(withJSONObject: ["content": content])
            return try await decode(
                ChatTurnEnvelope.self,
                request: request(
                    "api/v1/chats/\(conversationID)/messages",
                    method: "POST",
                    body: body
                )
            )
        }

        let boundary = "RiverBank-\(UUID().uuidString)"
        var body = Data()
        func append(_ value: String) {
            body.append(Data(value.utf8))
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"content\"\r\n")
        append("Content-Type: text/plain; charset=utf-8\r\n\r\n")
        append(content)
        append("\r\n")
        for attachment in attachments {
            let safeName = attachment.filename
                .replacingOccurrences(of: "\\", with: "_")
                .replacingOccurrences(of: "\"", with: "_")
                .replacingOccurrences(of: "\r", with: "_")
                .replacingOccurrences(of: "\n", with: "_")
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"files\"; filename=\"\(safeName)\"\r\n")
            append("Content-Type: \(attachment.mediaType)\r\n\r\n")
            body.append(attachment.data)
            append("\r\n")
        }
        append("--\(boundary)--\r\n")
        var uploadRequest = try request(
            "api/v1/chats/\(conversationID)/messages",
            method: "POST",
            body: body
        )
        uploadRequest.timeoutInterval = 120
        uploadRequest.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        return try await decode(ChatTurnEnvelope.self, request: uploadRequest)
    }

    func cancelChatMessage(conversationID: String, messageID: String) async throws -> ChatMessage {
        let body = try JSONSerialization.data(withJSONObject: [:])
        return try await decode(
            ChatMessageEnvelope.self,
            request: request(
                "api/v1/chats/\(conversationID)/messages/\(messageID)/cancel",
                method: "POST",
                body: body
            )
        ).message
    }
}
