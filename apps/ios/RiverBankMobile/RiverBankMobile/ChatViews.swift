import SwiftUI
import UIKit

private let chatCyan = Color(red: 0.337, green: 0.847, blue: 1.0)

struct ChatView: View {
    @EnvironmentObject private var store: AppStore
    @State private var draft = ""
    @State private var showHistory = false
    @State private var localError = ""
    @FocusState private var composerFocused: Bool

    private var isResponding: Bool {
        store.chatMessages.contains { $0.isAssistant && $0.isActive }
    }

    private var activeTitle: String {
        guard let id = store.activeChatID else { return "RiverBank" }
        return store.chats.first(where: { $0.id == id })?.title ?? "RiverBank"
    }

    var body: some View {
        NavigationStack {
            ChatTranscript(messages: store.chatMessages) { content in
                Task { await retry(content) }
            }
            .navigationTitle(activeTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showHistory = true } label: {
                        Image(systemName: "line.3.horizontal")
                    }
                    .accessibilityLabel("对话记录")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await startNewChat() } } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .accessibilityLabel("新对话")
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                ChatComposer(
                    text: $draft,
                    focused: $composerFocused,
                    isResponding: isResponding,
                    send: { Task { await send() } },
                    stop: { Task { await stop() } }
                )
            }
            .sheet(isPresented: $showHistory) {
                ChatHistoryView(isPresented: $showHistory)
                    .environmentObject(store)
            }
            .task {
                await store.refreshChats(showLoading: true)
                if store.activeChatID == nil, let first = store.chats.first {
                    await store.selectChat(first)
                } else {
                    await store.refreshChatMessages()
                }
                while !Task.isCancelled {
                    let delay = isResponding ? 0.55 : 2.5
                    try? await Task.sleep(for: .seconds(delay))
                    if isResponding { await store.refreshChatMessages() }
                }
            }
            .alert("Chat 操作失败", isPresented: .constant(!localError.isEmpty)) {
                Button("好") { localError = "" }
            } message: {
                Text(localError)
            }
        }
    }

    private func startNewChat() async {
        do {
            _ = try await store.newChat()
            draft = ""
            composerFocused = true
        } catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func send() async {
        let content = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty, !isResponding else { return }
        draft = ""
        do {
            try await store.sendChat(content)
        } catch {
            draft = content
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func stop() async {
        do { try await store.stopChatResponse() }
        catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func retry(_ content: String) async {
        guard !content.isEmpty, !isResponding else { return }
        do { try await store.sendChat(content) }
        catch {
            draft = content
            composerFocused = true
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }
}

private struct ChatTranscript: View {
    let messages: [ChatMessage]
    let retry: (String) -> Void

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if messages.isEmpty {
                    ChatWelcomeView()
                        .frame(maxWidth: .infinity)
                        .padding(.top, 88)
                } else {
                    LazyVStack(spacing: 22) {
                        ForEach(Array(messages.enumerated()), id: \.element.id) { index, message in
                            ChatMessageView(
                                message: message,
                                retryContent: precedingUserContent(at: index),
                                retry: retry
                            )
                                .id(message.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 18)
                    .padding(.bottom, 18)
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onChange(of: messages) { _, updated in
                guard let last = updated.last else { return }
                withAnimation(.easeOut(duration: 0.22)) {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
        .background(Color(.systemBackground))
    }

    private func precedingUserContent(at index: Int) -> String? {
        guard index > 0 else { return nil }
        return messages[..<index].last(where: { $0.role == "user" })?.content
    }
}

private struct ChatWelcomeView: View {
    var body: some View {
        VStack(spacing: 18) {
            Image("RiverBankMark")
                .resizable()
                .scaledToFit()
                .frame(width: 62, height: 62)
                .padding(12)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 22))
            Text("有什么可以帮你？")
                .font(.title2.weight(.semibold))
            Text("可以直接提问、讨论想法；复杂调研和文件整理可交给后台任务。")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 34)
        }
    }
}

private struct ChatMessageView: View {
    let message: ChatMessage
    let retryContent: String?
    let retry: (String) -> Void

    private var markdown: AttributedString {
        (try? AttributedString(markdown: message.content)) ?? AttributedString(message.content)
    }

    var body: some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 46)
                Text(message.content)
                    .textSelection(.enabled)
                    .padding(.horizontal, 15)
                    .padding(.vertical, 11)
                    .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 20))
            }
        } else {
            HStack(alignment: .top, spacing: 11) {
                Image("RiverBankMark")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 27, height: 27)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 9) {
                    if message.content.isEmpty && message.isActive {
                        ThinkingIndicator()
                    } else if !message.content.isEmpty {
                        Text(markdown)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if message.state == "failed" {
                        Label(message.error.isEmpty ? "回答失败" : message.error, systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(.red)
                    } else if message.state == "cancelled" {
                        Text("已停止生成")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if ["failed", "cancelled"].contains(message.state), let retryContent {
                        Button("重试") { retry(retryContent) }
                            .font(.caption.weight(.medium))
                    }
                    if !message.content.isEmpty && !message.isActive {
                        Button {
                            UIPasteboard.general.string = message.content
                        } label: {
                            Image(systemName: "doc.on.doc")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("复制回答")
                    }
                }
                Spacer(minLength: 12)
            }
        }
    }
}

private struct ThinkingIndicator: View {
    @State private var active = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(chatCyan)
                    .frame(width: 6, height: 6)
                    .opacity(active ? 1 : 0.28)
                    .animation(
                        .easeInOut(duration: 0.65).repeatForever().delay(Double(index) * 0.14),
                        value: active
                    )
            }
        }
        .frame(height: 27)
        .onAppear { active = true }
    }
}

private struct ChatComposer: View {
    @Binding var text: String
    var focused: FocusState<Bool>.Binding
    let isResponding: Bool
    let send: () -> Void
    let stop: () -> Void

    var body: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("询问任何问题", text: $text, axis: .vertical)
                .lineLimit(1...6)
                .focused(focused)
                .padding(.leading, 6)
                .padding(.vertical, 8)
                .submitLabel(.send)
                .onSubmit {
                    if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        send()
                    }
                }

            Button(action: isResponding ? stop : send) {
                Image(systemName: isResponding ? "stop.fill" : "arrow.up")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(.black)
                    .frame(width: 34, height: 34)
                    .background(
                        isResponding || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                            ? chatCyan : Color.secondary.opacity(0.25),
                        in: Circle()
                    )
            }
            .disabled(!isResponding && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel(isResponding ? "停止生成" : "发送")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 25))
        .overlay {
            RoundedRectangle(cornerRadius: 25)
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.7)
        }
        .padding(.horizontal, 12)
        .padding(.top, 7)
        .padding(.bottom, 6)
        .background(.bar)
    }
}

private struct ChatHistoryView: View {
    @EnvironmentObject private var store: AppStore
    @Binding var isPresented: Bool
    @State private var localError = ""

    var body: some View {
        NavigationStack {
            List {
                Button {
                    Task {
                        do {
                            _ = try await store.newChat()
                            isPresented = false
                        } catch {
                            localError = error.localizedDescription
                        }
                    }
                } label: {
                    Label("新对话", systemImage: "square.and.pencil")
                        .font(.headline)
                }
                ForEach(store.chats) { conversation in
                    Button {
                        Task {
                            await store.selectChat(conversation)
                            isPresented = false
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(conversation.title)
                                .font(.body.weight(.medium))
                                .lineLimit(1)
                            if let preview = conversation.preview, !preview.isEmpty {
                                Text(preview)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    .swipeActions {
                        Button(role: .destructive) {
                            Task {
                                do { try await store.deleteChat(conversation) }
                                catch { localError = error.localizedDescription }
                            }
                        } label: {
                            Label("删除", systemImage: "trash")
                        }
                    }
                }
            }
            .navigationTitle("对话")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { isPresented = false }
                }
            }
            .refreshable { await store.refreshChats() }
            .alert("操作失败", isPresented: .constant(!localError.isEmpty)) {
                Button("好") { localError = "" }
            } message: { Text(localError) }
        }
    }
}
