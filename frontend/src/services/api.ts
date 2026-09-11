import axios from "axios";
import { useAuth } from "../auth/AuthProvider";
import {
  Assessment, AssessmentSummary, Finding, FindingsByCategory, PreflightResponse, Subscription,
} from "../types";

const http = axios.create({ baseURL: "/api" });

export function useApi() {
  const { getToken } = useAuth();

  async function authHeaders() {
    return { Authorization: `Bearer ${getToken()}` };
  }

  return {
    async getSubscriptions(): Promise<Subscription[]> {
      const { data } = await http.get<Subscription[]>("/subscriptions/", {
        headers: await authHeaders(),
      });
      return data;
    },

    async preflight(subscriptionId: string): Promise<PreflightResponse> {
      const { data } = await http.get<PreflightResponse>("/assessments/preflight", {
        params: { subscription_id: subscriptionId },
        headers: await authHeaders(),
      });
      return data;
    },

    async createAssessment(subscriptionIds: string[]): Promise<AssessmentSummary> {
      const { data } = await http.post<AssessmentSummary>(
        "/assessments/",
        { subscription_ids: subscriptionIds },
        { headers: await authHeaders() }
      );
      return data;
    },

    async getAssessment(id: number): Promise<Assessment> {
      const { data } = await http.get<Assessment>(`/assessments/${id}`, {
        headers: await authHeaders(),
      });
      return data;
    },

    async listAssessments(): Promise<AssessmentSummary[]> {
      const { data } = await http.get<AssessmentSummary[]>("/assessments/", {
        headers: await authHeaders(),
      });
      return data;
    },

    async getFindingsByCategory(id: number): Promise<FindingsByCategory[]> {
      const { data } = await http.get<FindingsByCategory[]>(
        `/assessments/${id}/findings/by-category`,
        { headers: await authHeaders() }
      );
      return data;
    },

    async dismissFinding(assessmentId: number, findingId: number): Promise<Finding> {
      const { data } = await http.post<Finding>(
        `/assessments/${assessmentId}/findings/${findingId}/dismiss`,
        {},
        { headers: await authHeaders() }
      );
      return data;
    },

    async restoreFinding(assessmentId: number, findingId: number): Promise<Finding> {
      const { data } = await http.post<Finding>(
        `/assessments/${assessmentId}/findings/${findingId}/restore`,
        {},
        { headers: await authHeaders() }
      );
      return data;
    },

    async downloadReport(id: number): Promise<void> {
      const token = getToken();
      const response = await http.get(`/assessments/${id}/report/pdf`, {
        headers: { Authorization: `Bearer ${token}` },
        responseType: "blob",
      });

      const url = window.URL.createObjectURL(
        new Blob([response.data as BlobPart], { type: "application/pdf" })
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `assessment-${id}.pdf`;
      link.click();
      window.URL.revokeObjectURL(url);
    },
  };
}
