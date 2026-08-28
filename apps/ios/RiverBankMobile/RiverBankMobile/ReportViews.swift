import SwiftUI

struct ReportListView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        NavigationStack {
            Group {
                if store.reports.isEmpty && !store.isLoading {
                    ContentUnavailableView(
                        "暂无报告",
                        systemImage: "doc.text",
                        description: Text("后台任务完成后，Markdown 报告会出现在这里。")
                    )
                } else {
                    List(store.reports) { report in
                        NavigationLink(value: report) {
                            VStack(alignment: .leading, spacing: 7) {
                                Text(report.title).font(.headline).lineLimit(2)
                                if !report.summary.isEmpty {
                                    Text(report.summary)
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(3)
                                }
                                Text(report.filename)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.tertiary)
                            }
                            .padding(.vertical, 5)
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("报告库")
            .navigationDestination(for: ReportRecord.self) { report in
                ReportDetailView(report: report)
            }
            .refreshable { await store.refreshReports() }
            .task { await store.refreshReports(showLoading: true) }
        }
    }
}

struct ReportDetailView: View {
    @EnvironmentObject private var store: AppStore
    let report: ReportRecord
    @State private var content = ""
    @State private var shareURL: URL?
    @State private var loading = true
    @State private var localError = ""

    var body: some View {
        ScrollView {
            if loading {
                ProgressView("读取报告…").padding(.top, 80)
            } else {
                Text(markdown)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding()
            }
        }
        .navigationTitle(report.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                if let shareURL {
                    ShareLink(item: shareURL) {
                        Image(systemName: "square.and.arrow.up")
                    }
                }
                Button { Task { await prepareShare() } } label: {
                    Image(systemName: "arrow.down.circle")
                }
            }
        }
        .task { await load() }
        .alert("读取失败", isPresented: .constant(!localError.isEmpty)) {
            Button("好") { localError = "" }
        } message: { Text(localError) }
    }

    private var markdown: AttributedString {
        (try? AttributedString(
            markdown: content,
            options: .init(interpretedSyntax: .full)
        )) ?? AttributedString(content)
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do { content = try await store.reportContent(id: report.id).content }
        catch {
            if !error.isRiverBankCancellation {
                localError = error.localizedDescription
            }
        }
    }

    private func prepareShare() async {
        do { shareURL = try await store.download(report: report) }
        catch {
            if !error.isRiverBankCancellation {
                localError = error.localizedDescription
            }
        }
    }
}
